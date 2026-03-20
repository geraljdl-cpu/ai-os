#!/usr/bin/env python3
"""
doc_alerts.py — Document expiry alerts (Telegram + email).
Runs daily via systemd timer (07:30).
Checks expired and expiring-soon documents; sends alerts.
"""
import argparse
import json
import os
import sys

# Load env from /etc/aios.env before anything else
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "dbname=aios user=aios_user password=jdl host=127.0.0.1"
)
TG_TOKEN = os.environ.get("AIOS_TG_TOKEN", "")
TG_CHAT  = os.environ.get("AIOS_TG_CHAT", "")
SMTP_USER = os.environ.get("SMTP_USER", "joaodiogo@grupojdl.pt")

# email_send lives in bin/ — insert its directory into sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import psycopg2
from psycopg2.extras import RealDictCursor

from email_send import send_email  # noqa: E402 — must come after sys.path fix


# ── SQL ───────────────────────────────────────────────────────────────────────

_SQL = """
SELECT d.id, d.owner_type, d.owner_id, d.doc_type, d.title,
       d.expiry_date, d.status,
       d.expiry_date - CURRENT_DATE AS days_left,
       COALESCE(v.matricula, c.name, p.name, 'ID:' || d.owner_id::text) AS owner_name
FROM public.documents d
LEFT JOIN public.vehicles v ON d.owner_type = 'vehicle' AND d.owner_id = v.id
LEFT JOIN public.companies c ON d.owner_type = 'company' AND d.owner_id = c.id
LEFT JOIN public.persons p ON d.owner_type = 'person' AND d.owner_id = p.id
WHERE d.expiry_date IS NOT NULL
  AND d.expiry_date <= CURRENT_DATE + interval '{days} days'
ORDER BY d.expiry_date ASC
"""


# ── DB ────────────────────────────────────────────────────────────────────────

def _connect():
    # psycopg2 accepts both DSN strings and connection URIs
    dsn = DATABASE_URL
    return psycopg2.connect(dsn, cursor_factory=RealDictCursor)


def fetch_docs(days: int) -> list:
    sql = _SQL.format(days=days)
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        conn.close()


# ── TELEGRAM ──────────────────────────────────────────────────────────────────

def send_telegram(msg: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("[doc_alerts] Telegram not configured — skipping", file=sys.stderr)
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[doc_alerts] Telegram error: {e}", file=sys.stderr)
        return False


def _owner_label(row) -> str:
    owner = row.get("owner_name") or ""
    otype = row.get("owner_type") or ""
    if otype == "vehicle":
        return f"Viatura {owner}"
    if otype == "company":
        return f"Empresa {owner}"
    if otype == "person":
        return owner
    return owner or f"ID:{row.get('owner_id','?')}"


def _doc_label(row) -> str:
    return row.get("doc_type") or row.get("title") or "Documento"


def _days_label(days_left: int) -> str:
    if days_left < 0:
        n = abs(days_left)
        return f"expirou há {n} dia{'s' if n != 1 else ''}"
    if days_left == 0:
        return "expira hoje"
    return f"expira em {days_left} dia{'s' if days_left != 1 else ''}"


def build_telegram_message(expired: list, expiring: list) -> str:
    lines = []

    if expired:
        lines.append(f"*DOCUMENTOS EXPIRADOS ({len(expired)}):*")
        for row in expired:
            owner  = _owner_label(row)
            doc    = _doc_label(row)
            label  = _days_label(int(row["days_left"]))
            lines.append(f"• {owner} — {doc} ({label})")
        lines.append("")

    if expiring:
        lines.append(f"*A EXPIRAR {expiring[0].get('_threshold',30)} DIAS ({len(expiring)}):*")
        for row in expiring:
            owner  = _owner_label(row)
            doc    = _doc_label(row)
            days   = int(row["days_left"])
            lines.append(f"• {owner} — {doc} ({days} dia{'s' if days != 1 else ''})")

    return "\n".join(lines)


# ── EMAIL ─────────────────────────────────────────────────────────────────────

def build_email_html(expired: list) -> str:
    rows_html = ""
    for row in expired:
        days_left = int(row["days_left"])
        color = "#d9534f" if days_left < 0 else "#f0ad4e"
        rows_html += (
            f"<tr>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{row.get('doc_type','')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{_owner_label(row)}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{row.get('title','')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee'>{row.get('expiry_date','')}</td>"
            f"<td style='padding:6px 10px;border-bottom:1px solid #eee;color:{color};font-weight:bold'>"
            f"{days_left}</td>"
            f"</tr>"
        )
    return f"""
<html><body style='font-family:sans-serif;color:#333'>
<h2 style='color:#d9534f'>Documentos Expirados</h2>
<table style='border-collapse:collapse;width:100%;max-width:800px'>
  <thead>
    <tr style='background:#f5f5f5'>
      <th style='padding:8px 10px;text-align:left'>Tipo</th>
      <th style='padding:8px 10px;text-align:left'>Owner</th>
      <th style='padding:8px 10px;text-align:left'>Título</th>
      <th style='padding:8px 10px;text-align:left'>Data Expiração</th>
      <th style='padding:8px 10px;text-align:left'>Dias</th>
    </tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>
<p style='color:#888;margin-top:24px;font-size:12px'>AI-OS — alertas automáticos</p>
</body></html>
"""


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Document expiry alerts")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print alerts but do not send")
    parser.add_argument("--days", type=int, default=30,
                        help="Days ahead to look for expiring docs (default: 30)")
    args = parser.parse_args()

    try:
        all_docs = fetch_docs(args.days)
    except Exception as e:
        print(f"[doc_alerts] DB error: {e}", file=sys.stderr)
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    expired  = [r for r in all_docs if int(r["days_left"]) < 0]
    expiring = [r for r in all_docs if 0 <= int(r["days_left"]) <= args.days]

    # Annotate with threshold for message formatting
    for r in expiring:
        r["_threshold"] = args.days

    notifications_sent = 0

    if not expired and not expiring:
        print("[doc_alerts] No expired or expiring documents found.")
        print(json.dumps({
            "ok": True,
            "expired_count": 0,
            "expiring_count": 0,
            "notifications_sent": 0,
        }))
        return

    # ── Print summary ─────────────────────────────────────────────────────────
    if expired:
        print(f"[doc_alerts] EXPIRED ({len(expired)}):")
        for row in expired:
            print(f"  - {_owner_label(row)} — {_doc_label(row)} "
                  f"({row['expiry_date']}, {row['days_left']}d)")

    if expiring:
        print(f"[doc_alerts] EXPIRING in {args.days}d ({len(expiring)}):")
        for row in expiring:
            print(f"  - {_owner_label(row)} — {_doc_label(row)} "
                  f"({row['expiry_date']}, {row['days_left']}d)")

    tg_msg = build_telegram_message(expired, expiring)
    print("\n[doc_alerts] Telegram message:")
    print(tg_msg)

    if not args.dry_run:
        # ── Send Telegram ─────────────────────────────────────────────────────
        ok = send_telegram(tg_msg)
        if ok:
            notifications_sent += 1
            print("[doc_alerts] Telegram sent OK")

        # ── Send email (only if there are expired docs) ───────────────────────
        if expired:
            email_to   = SMTP_USER or "joaodiogo@grupojdl.pt"
            subject    = f"Documentos expirados — {len(expired)} urgentes"
            body_html  = build_email_html(expired)
            result     = send_email(email_to, subject, body_html)
            if result.get("ok"):
                notifications_sent += 1
                print(f"[doc_alerts] Email sent to {email_to}")
            else:
                print(f"[doc_alerts] Email error: {result.get('error')}", file=sys.stderr)
    else:
        print("[doc_alerts] DRY RUN — no notifications sent")

    print(json.dumps({
        "ok": True,
        "expired_count": len(expired),
        "expiring_count": len(expiring),
        "notifications_sent": notifications_sent,
    }))


if __name__ == "__main__":
    main()
