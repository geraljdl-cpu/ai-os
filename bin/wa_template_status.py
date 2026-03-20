#!/usr/bin/env python3
"""
wa_template_status.py — Estado dos templates WhatsApp no Twilio
Uso: python3 bin/wa_template_status.py
"""
import json, os, sys
import requests

# ── Credenciais ───────────────────────────────────────────────────────────────
for _env_file in ("/etc/aios.env", os.path.expanduser("~/.env.db")):
    if os.path.exists(_env_file):
        for _line in open(_env_file):
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _, _v = _line.partition("=")
                os.environ.setdefault(_k.strip(), _v.strip())

ACCT = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH = os.environ.get("TWILIO_AUTH_TOKEN",  "")

if not (ACCT and AUTH):
    sys.exit("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN não definidos")

# ── Templates a verificar ─────────────────────────────────────────────────────
SIDS = [
    "HX24668142906f01795c506e87521fc35d",  # client_validation_notice_pt
    "HX61cab303903376eb3a00db1122178302",  # client_checkin_notice_pt
]

BASE = "https://content.twilio.com/v1"

def fetch(sid: str) -> dict:
    c  = requests.get(f"{BASE}/Content/{sid}",                  auth=(ACCT, AUTH), timeout=10)
    ar = requests.get(f"{BASE}/Content/{sid}/ApprovalRequests", auth=(ACCT, AUTH), timeout=10)
    c.raise_for_status(); ar.raise_for_status()
    return {**c.json(), "approval": ar.json().get("whatsapp", {})}

# ── Formatação ────────────────────────────────────────────────────────────────
STATUS_LABEL = {
    "approved":   "✅ READY",
    "received":   "⏳ WAITING",
    "pending":    "⏳ WAITING",
    "rejected":   "❌ REJECTED",
    "unsubmitted":"🔘 NOT SUBMITTED",
}

rows = []
for sid in SIDS:
    try:
        d     = fetch(sid)
        wa    = d["approval"]
        name  = d.get("friendly_name", "?")
        status = wa.get("status", "unknown")
        label  = STATUS_LABEL.get(status, f"? {status}")
        reason = wa.get("rejection_reason", "").strip()
        rows.append((name, sid, status, label, reason))
    except Exception as e:
        rows.append(("ERROR", sid, "error", f"❌ {e}", ""))

# ── Tabela ────────────────────────────────────────────────────────────────────
COL = [28, 38, 12, 14]
sep = "+" + "+".join("-" * (c + 2) for c in COL) + "+"
hdr = "| {:<28} | {:<38} | {:<12} | {:<14} |".format(
    "friendly_name", "content_sid", "wa_status", "resultado"
)
print(sep)
print(hdr)
print(sep)
for name, sid, status, label, reason in rows:
    print("| {:<28} | {:<38} | {:<12} | {:<14} |".format(
        name[:28], sid, status[:12], label[:14]
    ))
    if reason:
        print(f"|   ↳ REJEIÇÃO: {reason}")
print(sep)

all_ready = all(r[2] == "approved" for r in rows)
print()
if all_ready:
    print("🟢 Todos os templates aprovados — sistema pronto para envio fora da janela 24h.")
else:
    print("🟡 Aguardar aprovação Meta antes de enviar fora da janela 24h.")
    print("   (templates UTILITY normalmente aprovados em minutos a horas)")
