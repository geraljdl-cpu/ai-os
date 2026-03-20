#!/usr/bin/env python3
"""
insurance_alerts.py — Daily insurance renewal/expiry alerts.
Runs daily at 07:30 via systemd timer.

Flow:
  1. generate_alerts() — create pending alert rows (30/15/7/1 days before renewal/end)
  2. Query pending alerts → send Telegram + email summary
  3. Mark alerts as sent
"""
import argparse
import datetime as dt
import json
import os
import sys

# Load env
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
TG_TOKEN     = os.environ.get("AIOS_TG_TOKEN", "")
TG_CHAT      = os.environ.get("AIOS_TG_CHAT", "")
SMTP_USER    = os.environ.get("SMTP_USER", "joaodiogo@grupojdl.pt")

# email_send is in bin/
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _send_telegram(msg: str) -> bool:
    if not TG_TOKEN or not TG_CHAT:
        print("[insurance_alerts] Telegram não configurado", file=sys.stderr)
        return False
    try:
        import urllib.request
        data = json.dumps({"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"}).encode()
        req  = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
        return True
    except Exception as e:
        print(f"[insurance_alerts] Telegram error: {e}", file=sys.stderr)
        return False


def run(dry_run: bool = False):
    # ── import engine ──────────────────────────────────────────────────────────
    _bin = os.path.dirname(os.path.abspath(__file__))
    if _bin in sys.path:
        sys.path.remove(_bin)
    sys.path.insert(0, _bin)
    from insurance_engine import _conn, generate_alerts, list_alerts

    engine, sa_text = _conn()

    # 1. Generate new alert rows
    gen = generate_alerts(engine, sa_text)
    print(f"[insurance_alerts] Alertas gerados: {gen['created']} novos, {gen['expired']} expiradas")

    # 2. Fetch pending alerts
    alerts = list_alerts(engine, sa_text, status="pending", limit=50)
    if not alerts:
        print("[insurance_alerts] Sem alertas pendentes hoje")
        return

    today = dt.date.today()

    # 3. Send Telegram
    lines = [f"🛡️ *Seguros — Alertas {today.strftime('%d/%m/%Y')}*\n"]
    for a in alerts:
        ref_date = a.get("renewal_date") or a.get("end_date") or "?"
        if ref_date and ref_date != "?":
            days_left = (dt.date.fromisoformat(str(ref_date)[:10]) - today).days
            days_str  = f"{days_left}d" if days_left >= 0 else "EXPIRADA"
        else:
            days_str = "?"
        entity  = a.get("entity_ref") or "—"
        insurer = a.get("insurer_name") or "—"
        cat     = a.get("category") or "—"
        lines.append(f"• {insurer} — {entity} ({cat}) — vence em {days_str}")

    tg_msg = "\n".join(lines)
    if not dry_run:
        _send_telegram(tg_msg)

    # 4. Send email summary
    rows_html = ""
    for a in alerts:
        ref_date = a.get("renewal_date") or a.get("end_date") or "?"
        if ref_date and ref_date != "?":
            days_left = (dt.date.fromisoformat(str(ref_date)[:10]) - today).days
            days_str  = f"{days_left} dias" if days_left >= 0 else "EXPIRADA"
        else:
            days_str = "?"
        rows_html += f"""
        <tr>
          <td>{a.get('insurer_name','—')}</td>
          <td>{a.get('entity_ref','—')}</td>
          <td>{a.get('category','—')}</td>
          <td>{ref_date}</td>
          <td><b>{days_str}</b></td>
          <td>{a.get('alert_type','—')}</td>
        </tr>"""

    email_html = f"""
    <h2>🛡️ Seguros — Alertas {today.strftime('%d/%m/%Y')}</h2>
    <table border="1" cellpadding="6" style="border-collapse:collapse;font-family:sans-serif">
      <thead><tr style="background:#1e3a5f;color:white">
        <th>Seguradora</th><th>Referência</th><th>Categoria</th>
        <th>Data Ref.</th><th>Prazo</th><th>Tipo Alerta</th>
      </tr></thead>
      <tbody>{rows_html}</tbody>
    </table>
    <p style="color:#999;font-size:11px">AIOS Insurance Alerts — {today}</p>
    """

    if not dry_run:
        try:
            from email_send import send_email
            send_email(
                to=SMTP_USER,
                subject=f"[AIOS] Seguros — {len(alerts)} alertas {today.strftime('%d/%m/%Y')}",
                body_html=email_html,
            )
        except Exception as e:
            print(f"[insurance_alerts] Email error: {e}", file=sys.stderr)

    # 5. Mark as sent
    if not dry_run:
        import sqlalchemy as sa
        with engine.begin() as conn:
            ids = [a["id"] for a in alerts]
            conn.execute(
                sa.text("UPDATE public.insurance_alerts SET status='sent', sent_telegram_at=NOW() WHERE id = ANY(:ids)"),
                {"ids": ids}
            )
        print(f"[insurance_alerts] {len(alerts)} alertas marcados como 'sent'")
    else:
        print(f"[insurance_alerts] DRY RUN — {len(alerts)} alertas (não enviados)")
        print(tg_msg)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
