#!/usr/bin/env python3
"""
finance_alerts.py — alertas Telegram para obrigações fiscais próximas.
Corre via systemd timer (diário 08:30).
Avisa: 10 dias, 5 dias, 2 dias, no próprio dia.
Anti-spam: só envia uma vez por obrigação por janela de alerta.
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import requests
from datetime import datetime, timezone, date

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
TG_TOKEN     = os.environ.get("AIOS_TG_TOKEN", "")
TG_CHAT      = os.environ.get("AIOS_TG_CHAT", "")

ALERT_WINDOWS = [10, 5, 2, 0]  # dias de antecedência


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10
        )
    except Exception as e:
        print(f"[finance_alerts] telegram error: {e}", file=sys.stderr)


def already_alerted(c, text, ob_id: int, days: int) -> bool:
    """Verifica se já enviámos alerta para esta obrigação nesta janela."""
    key = f"finance_alert_{ob_id}_{days}d"
    row = c.execute(text("""
        SELECT 1 FROM public.events
        WHERE source = 'finance_alert'
          AND kind   = :key
          AND ts > NOW() - INTERVAL '20 hours'
        LIMIT 1
    """), {"key": key}).first()
    return row is not None


def record_alert(c, text, ob_id: int, days: int, label: str):
    key = f"finance_alert_{ob_id}_{days}d"
    c.execute(text("""
        INSERT INTO public.events (ts, level, source, kind, message, data)
        VALUES (NOW(), 'warn', 'finance_alert', :key, :msg, :data)
    """), {"key": key,
           "msg": f"Alerta fiscal: {label} em {days}d",
           "data": json.dumps({"obligation_id": ob_id, "days": days, "label": label})})


def main():
    engine, text = _conn()
    today = date.today()

    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, label, entity, due_date, amount, type,
                   (due_date - CURRENT_DATE) AS days_left
            FROM public.finance_obligations
            WHERE status = 'pending'
              AND due_date BETWEEN CURRENT_DATE - INTERVAL '1 day'
                               AND CURRENT_DATE + INTERVAL '11 days'
            ORDER BY due_date ASC
        """)).mappings().all()

    if not rows:
        print("[finance_alerts] nothing to alert")
        return

    with engine.begin() as c:
        for row in rows:
            days_left = int(row["days_left"])
            for window in ALERT_WINDOWS:
                if days_left != window:
                    continue
                ob_id = row["id"]
                label = row["label"]
                if already_alerted(c, text, ob_id, window):
                    print(f"[finance_alerts] skip (already alerted): {label} {window}d")
                    continue

                if window == 0:
                    urgency = "🔴 *HOJE*"
                elif window <= 2:
                    urgency = f"🟠 *{window} dias*"
                elif window <= 5:
                    urgency = f"🟡 *{window} dias*"
                else:
                    urgency = f"🔵 *{window} dias*"

                amount_str = ""
                if row["amount"]:
                    amount_str = f"\n💶 *Valor:* {float(row['amount']):,.2f} €"

                msg = (
                    f"📅 *Obrigação Fiscal*\n"
                    f"{urgency} — {label}\n"
                    f"🏛 *Entidade:* {row['entity']}\n"
                    f"📆 *Prazo:* {row['due_date']}"
                    f"{amount_str}\n\n"
                    f"_Acede a /finance para gerir._"
                )
                send_tg(msg)
                record_alert(c, text, ob_id, window, label)
                print(f"[finance_alerts] sent: {label} ({window}d)")


if __name__ == "__main__":
    import sys
    # Carregar env vars do ~/.env.db se existir
    env_file = os.path.expanduser("~/.env.db")
    if os.path.exists(env_file):
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
        # Re-lê variáveis depois de carregar o ficheiro
        TG_TOKEN = os.environ.get("AIOS_TG_TOKEN", "")
        TG_CHAT  = os.environ.get("AIOS_TG_CHAT", "")
    main()
