#!/usr/bin/env python3
"""
email_send.py — SMTP email sender (Zoho)

Usage:
  python3 bin/email_send.py <to> <subject> <body_text>
  python3 bin/email_send.py --html <to> <subject> <body_html>
  from bin.email_send import send_email
"""
import os, smtplib, sys, json
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.zoho.eu")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USER)


def _extract_email(addr: str) -> str:
    """Extract bare email from 'Name <email>' or plain email."""
    if "<" in addr and ">" in addr:
        return addr.split("<")[1].split(">")[0].strip()
    return addr.strip()


def send_email(to: str, subject: str, body_html: str, body_text: str = "") -> dict:
    """Send email via SMTP. Returns {ok, error?}."""
    if not SMTP_USER or not SMTP_PASS:
        return {"ok": False, "error": "SMTP not configured (SMTP_USER/SMTP_PASS missing)"}
    if not to:
        return {"ok": False, "error": "No recipient"}
    try:
        sender_bare = _extract_email(SMTP_FROM) or SMTP_USER
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = sender_bare
        msg["To"]      = to
        if body_text:
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
        msg.attach(MIMEText(body_html, "html", "utf-8"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(sender_bare, [to], msg.as_string())
        return {"ok": True, "to": to, "subject": subject}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send_email_plain(to: str, subject: str, body: str) -> dict:
    """Send plain text email."""
    html = f"<pre style='font-family:sans-serif'>{body}</pre>"
    return send_email(to, subject, html, body)


if __name__ == "__main__":
    args = sys.argv[1:]
    html_mode = "--html" in args
    if html_mode:
        args.remove("--html")
    if len(args) < 3:
        print("Usage: email_send.py [--html] <to> <subject> <body>", file=sys.stderr)
        sys.exit(1)
    to, subject, body = args[0], args[1], args[2]
    if html_mode:
        result = send_email(to, subject, body)
    else:
        result = send_email_plain(to, subject, body)
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["ok"] else 1)
