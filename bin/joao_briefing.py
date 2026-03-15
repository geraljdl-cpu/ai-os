#!/usr/bin/env python3
"""
joao_briefing.py — briefing diário do Painel do João.
08:30 — resumo matinal
18:00 — fecho do dia

Uso:
  python3 bin/joao_briefing.py morning
  python3 bin/joao_briefing.py evening
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import sys
import requests
from datetime import date

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
TG_TOKEN     = os.environ.get("AIOS_TG_TOKEN", "")
TG_CHAT      = os.environ.get("AIOS_TG_CHAT", "")
UI_BASE      = os.environ.get("AIOS_UI_BASE", "http://localhost:3000")


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def send_tg(msg: str):
    if not TG_TOKEN or not TG_CHAT:
        print(msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[joao_briefing] telegram error: {e}", file=sys.stderr)
        print(msg)


def get_obligations(engine, text, days=10, limit=5):
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT label, due_date, amount,
                   (due_date - CURRENT_DATE) AS days_left
            FROM public.finance_obligations
            WHERE status IN ('pending','approved')
              AND due_date <= CURRENT_DATE + :days
            ORDER BY due_date ASC LIMIT :lim
        """), {"days": days, "lim": limit}).mappings().all()
    return list(rows)


def get_decisions(engine, text, limit=5):
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT kind, title FROM public.decision_queue
            WHERE status = 'pending'
            ORDER BY created_at DESC LIMIT :lim
        """), {"lim": limit}).mappings().all()
    return list(rows)


def get_ideas(engine, text, limit=3):
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, title, status FROM public.idea_threads
            WHERE status IN ('open','analyzed')
            ORDER BY created_at DESC LIMIT :lim
        """), {"lim": limit}).mappings().all()
    return list(rows)


def get_timesheets_today(engine, text):
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT COUNT(*) AS cnt, COALESCE(SUM(hours),0) AS total_hours
            FROM public.event_timesheets
            WHERE status IN ('submitted','approved')
              AND DATE(created_at) = CURRENT_DATE
        """)).mappings().first()
    return row


def get_payout_week(engine, text):
    """Total de pagamentos desta semana."""
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT COALESCE(SUM(amount),0) AS total,
                   COUNT(*) AS workers
            FROM public.finance_payouts
            WHERE status = 'pending'
              AND week_start = date_trunc('week', CURRENT_DATE)
        """)).mappings().first()
    return row


def morning_briefing():
    engine, text = _conn()
    today = date.today().strftime("%A, %d %B %Y")

    obls     = get_obligations(engine, text, days=10)
    decs     = get_decisions(engine, text)
    ideas    = get_ideas(engine, text)
    payouts  = get_payout_week(engine, text)

    lines = [f"🌅 *Bom dia, João!*", f"_{today}_", ""]

    # obrigações
    if obls:
        lines.append("💰 *Obrigações próximas:*")
        for o in obls:
            days = int(o["days_left"])
            icon = "🔴" if days <= 0 else "🟠" if days <= 5 else "🟡"
            amt  = f" — {float(o['amount']):.0f}€" if o["amount"] else ""
            lines.append(f"{icon} {o['label']}{amt} ({days}d)")
        lines.append("")

    # pagamentos RH
    if payouts and float(payouts["total"]) > 0:
        lines.append(f"👷 *Pagamentos RH esta semana:*")
        lines.append(f"  {payouts['workers']} workers — {float(payouts['total']):.2f}€ pendente")
        lines.append("")

    # decisões
    if decs:
        lines.append("⚡ *Decisões pendentes:*")
        for d in decs[:3]:
            lines.append(f"  • {d['title']}")
        lines.append("")

    # ideias
    if ideas:
        lines.append("💡 *Ideias recentes:*")
        for i in ideas:
            icon = "📊" if i["status"] == "analyzed" else "💭"
            lines.append(f"  {icon} {i['title']}")
        lines.append("")

    lines.append(f"📋 Ver painel: {UI_BASE}/joao")

    send_tg("\n".join(lines))


def evening_briefing():
    engine, text = _conn()
    today = date.today().strftime("%d/%m/%Y")

    ts      = get_timesheets_today(engine, text)
    decs    = get_decisions(engine, text)
    ideas   = get_ideas(engine, text, limit=5)
    obls    = get_obligations(engine, text, days=3)

    lines = [f"🌆 *Fecho do dia — {today}*", ""]

    # horas hoje
    if ts and int(ts["cnt"]) > 0:
        lines.append(f"⏱ *Horas registadas hoje:*")
        lines.append(f"  {ts['cnt']} timesheets — {float(ts['total_hours']):.2f}h total")
        lines.append("")

    # obrigações urgentes
    urgent = [o for o in obls if int(o["days_left"]) <= 3]
    if urgent:
        lines.append("🔴 *Obrigações em 3 dias:*")
        for o in urgent:
            days = int(o["days_left"])
            lines.append(f"  • {o['label']} ({days}d)")
        lines.append("")

    # decisões por resolver
    if decs:
        lines.append(f"⚡ *{len(decs)} decisões pendentes*")
        for d in decs[:3]:
            lines.append(f"  • {d['title']}")
        lines.append("")

    # ideias por decidir
    if ideas:
        lines.append(f"💡 *{len(ideas)} ideias em aberto*")
        for i in ideas[:3]:
            icon = "📊" if i["status"] == "analyzed" else "💭"
            lines.append(f"  {icon} {i['title']}")
        lines.append("")

    lines.append("Boa noite! 👋")

    send_tg("\n".join(lines))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    if mode == "morning":
        morning_briefing()
    elif mode in ("evening", "evening"):
        evening_briefing()
    else:
        print(f"uso: joao_briefing.py morning|evening", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
