#!/usr/bin/env python3
"""
payout_run.py — cálculo semanal de pagamentos RH.
Corre todas as segundas-feiras via systemd timer ou manualmente.

Uso:
  python3 bin/payout_run.py [YYYY-MM-DD]   # semana explícita
  python3 bin/payout_run.py                # semana atual (segunda-feira)

O script:
  1. Calcula segunda-feira da semana corrente (ou usa data fornecida)
  2. Soma horas aprovadas por worker nessa semana
  3. Cria/actualiza registos em finance_payouts
  4. Envia resumo Telegram
  5. Regista evento no Twin
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import sys
import requests
from datetime import date, timedelta

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
TG_TOKEN     = os.environ.get("AIOS_TG_TOKEN", "")
TG_CHAT      = os.environ.get("AIOS_TG_CHAT", "")


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[payout_run] telegram error: {e}", file=sys.stderr)


def run(week_start: date):
    engine, text = _conn()
    with engine.begin() as c:
        rows = c.execute(text("""
            SELECT t.worker_id,
                   COALESCE(p.hourly_rate, t.hourly_rate, 0) AS rate,
                   SUM(t.hours) AS total_hours
            FROM public.event_timesheets t
            LEFT JOIN public.people p ON LOWER(p.name) = LOWER(t.worker_id)
            WHERE t.status = 'approved'
              AND t.start_time >= CAST(:ws AS date)
              AND t.start_time <  CAST(:ws AS date) + INTERVAL '7 days'
            GROUP BY t.worker_id, p.hourly_rate, t.hourly_rate
        """), {"ws": str(week_start)}).mappings().all()

        results = []
        for r in rows:
            hours  = float(r["total_hours"] or 0)
            rate   = float(r["rate"] or 0)
            amount = round(hours * rate, 2)

            existing = c.execute(text("""
                SELECT id FROM public.finance_payouts
                WHERE worker_id = :wid AND week_start = :ws
            """), {"wid": r["worker_id"], "ws": str(week_start)}).first()

            if existing:
                c.execute(text("""
                    UPDATE public.finance_payouts
                    SET total_hours = :h, amount = :a, updated_at = NOW()
                    WHERE id = :id
                """), {"h": hours, "a": amount, "id": existing[0]})
            else:
                c.execute(text("""
                    INSERT INTO public.finance_payouts (worker_id, week_start, total_hours, amount)
                    VALUES (:wid, :ws, :h, :a)
                """), {"wid": r["worker_id"], "ws": str(week_start), "h": hours, "a": amount})

            results.append({"worker": r["worker_id"], "hours": hours, "amount": amount})

        total = sum(x["amount"] for x in results)

        # evento Twin
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'payout_run', 'payout_run_created',
                    :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Payout run {week_start}: {len(results)} workers, total {total:.2f}€",
            "data": json.dumps({"week_start": str(week_start), "workers": results, "total": total})
        })

    return results, total


def main():
    if len(sys.argv) > 1:
        week_start = date.fromisoformat(sys.argv[1])
    else:
        week_start = monday_of_week(date.today())

    print(f"[payout_run] semana: {week_start}", file=sys.stderr)
    results, total = run(week_start)

    if not results:
        print(f"[payout_run] sem horas aprovadas para semana {week_start}")
        return

    # output tabela
    lines = [f"*Pagamentos RH — semana {week_start}*", ""]
    for r in sorted(results, key=lambda x: -x["amount"]):
        lines.append(f"  {r['worker']}: {r['hours']}h → *{r['amount']:.2f}€*")
    lines.append("")
    lines.append(f"  *Total: {total:.2f}€*")
    msg = "\n".join(lines)

    print(msg.replace("*", ""))
    send_tg(msg)


if __name__ == "__main__":
    main()
