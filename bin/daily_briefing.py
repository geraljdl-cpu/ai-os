#!/usr/bin/env python3
"""
daily_briefing.py — Morning briefing 08:30
Fetches from all 6 sources, sends Telegram + saves to agent_suggestions.
Format: short, operational, no walls of text.
"""
import sys, os, json, datetime as dt, urllib.request

import psycopg2
from psycopg2.extras import RealDictCursor

DSN      = os.environ.get("DATABASE_URL", "dbname=aios user=aios_user password=jdl host=127.0.0.1")
TG_TOKEN = None
TG_CHAT  = None

_env_file = os.path.expanduser("~/.env.db")
if os.path.exists(_env_file):
    for line in open(_env_file):
        line = line.strip()
        if line.startswith("AIOS_TG_TOKEN="):
            TG_TOKEN = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("AIOS_TG_CHAT="):
            TG_CHAT = line.split("=", 1)[1].strip().strip('"').strip("'")


def db():
    return psycopg2.connect(DSN, cursor_factory=RealDictCursor)


def q(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def q1(conn, sql, params=()):
    rows = q(conn, sql, params)
    return rows[0] if rows else {}


def send_telegram(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        body = json.dumps({"chat_id": TG_CHAT, "text": text, "parse_mode": "Markdown"}).encode()
        req  = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=body, headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"[briefing] telegram error: {e}", file=sys.stderr)


def build_briefing(conn) -> str:
    today = dt.date.today()
    now   = today
    lines = [f"*Briefing {today.strftime('%d/%m/%Y')} 08:30*", ""]

    # ── PRIORIDADES (3 items) ──────────────────────────────────────────────────
    priorities = []

    # 1. Finance obligations due ≤ 7 days
    obs = q(conn, """
        SELECT label, due_date, amount FROM public.finance_obligations
        WHERE status NOT IN ('paid','cancelled')
          AND due_date <= CURRENT_DATE + 7
        ORDER BY due_date ASC LIMIT 3
    """)
    for ob in obs:
        days = (ob["due_date"] - now).days
        amt  = f" ({float(ob['amount']):.0f}€)" if ob.get("amount") else ""
        priorities.append(("💶", f"{ob['label']}{amt} — {days}d", 10))

    # 2. Expired/expiring docs
    docs = q(conn, """
        SELECT title, doc_type, status, expiry_date FROM public.documents
        WHERE status IN ('expired','expiring')
        ORDER BY expiry_date ASC NULLS LAST LIMIT 2
    """)
    for d in docs:
        if d["status"] == "expired":
            priorities.append(("🔴", f"Doc expirado: {d['doc_type']}", 9))
        else:
            days = (d["expiry_date"] - now).days if d["expiry_date"] else "?"
            priorities.append(("🟡", f"Doc expira {days}d: {d['doc_type']}", 7))

    # 3. Open approvals
    appr = q1(conn, "SELECT COUNT(*) AS n FROM public.twin_approvals WHERE status='pending'")
    if appr.get("n", 0) > 0:
        priorities.append(("✅", f"{appr['n']} aprovações pendentes", 8))

    # 4. Critical incidents
    crit = q1(conn, "SELECT COUNT(*) AS n FROM public.incidents WHERE status='open' AND severity='crit'")
    if crit.get("n", 0) > 0:
        priorities.append(("🚨", f"{crit['n']} incidentes críticos", 10))

    priorities.sort(key=lambda x: -x[2])
    lines.append("*Prioridades:*")
    for icon, text, _ in priorities[:3]:
        lines.append(f"  {icon} {text}")
    if not priorities:
        lines.append("  ✓ Sem prioridades críticas")

    lines.append("")

    # ── DECISÃO CRÍTICA ────────────────────────────────────────────────────────
    dec = q1(conn, """
        SELECT title FROM public.agent_suggestions
        WHERE kind='alert' AND is_read=false
        ORDER BY score DESC, created_at DESC LIMIT 1
    """)
    lines.append("*Decisão crítica:*")
    lines.append(f"  {'→ ' + dec['title'] if dec else '—'}")
    lines.append("")

    # ── RADAR ─────────────────────────────────────────────────────────────────
    tender = q1(conn, """
        SELECT name, metadata FROM public.twin_entities
        WHERE type='tender' AND status='active'
        ORDER BY (COALESCE(metadata->>'score','0'))::int DESC LIMIT 1
    """)
    lines.append("*Radar:*")
    if tender:
        meta  = tender.get("metadata") or {}
        score = meta.get("score", "?") if isinstance(meta, dict) else "?"
        lines.append(f"  ⚡ {tender['name'][:60]} (score {score})")
    else:
        lines.append("  — sem concursos activos")
    lines.append("")

    # ── CASES ABERTOS ─────────────────────────────────────────────────────────
    cases = q1(conn, """
        SELECT COUNT(*) AS n FROM public.twin_cases WHERE status='open'
    """)
    tasks_pending = q1(conn, """
        SELECT COUNT(*) AS n FROM public.twin_tasks WHERE status='pending'
    """)
    lines.append("*Operações:*")
    lines.append(f"  Cases abertos: {cases.get('n',0)}  |  Tarefas pendentes: {tasks_pending.get('n',0)}")

    # ── INCIDENTS SUMMARY ─────────────────────────────────────────────────────
    inc = q1(conn, """
        SELECT
          COUNT(*) FILTER (WHERE severity='crit') AS crit,
          COUNT(*) FILTER (WHERE severity='warn') AS warn,
          COUNT(*) FILTER (WHERE severity='info') AS info
        FROM public.incidents WHERE status='open'
    """)
    lines.append(f"  Incidentes: 🔴{inc.get('crit',0)} 🟡{inc.get('warn',0)} ℹ{inc.get('info',0)}")

    lines.append("")
    lines.append("_AI-OS — autonomia operacional_")

    return "\n".join(lines)


def run():
    conn = db()
    try:
        briefing = build_briefing(conn)
        print(briefing)

        # Save to agent_suggestions
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO public.agent_suggestions(kind, title, details, score)
                VALUES ('briefing', %s, %s, 0)
            """, (f"Briefing 0830 {dt.date.today().isoformat()}", briefing))
        conn.commit()

        send_telegram(briefing)
    finally:
        conn.close()


if __name__ == "__main__":
    run()
