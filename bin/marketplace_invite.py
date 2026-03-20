#!/usr/bin/env python3
"""
marketplace_invite.py — Envia convite WhatsApp a worker para marketplace_job.

Completamente isolado do fluxo Mauro/Ryan/Oneway.
Não toca em event_timesheets, whatsapp_handler, worker_contacts.

Uso:
  python3 bin/marketplace_invite.py --job-id 1 --worker-id 2
  python3 bin/marketplace_invite.py --job-id 1        # convida todos os 'invited'

Output: JSON por linha {"event": ..., "result": ...}
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, datetime as dt, json, os, sys
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

# ── Env ───────────────────────────────────────────────────────────────────────
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip()

DSN           = os.environ.get("DATABASE_URL", "dbname=aios user=aios_user password=jdl host=127.0.0.1")
ACCOUNT_SID   = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN    = os.environ.get("TWILIO_AUTH_TOKEN", "")
WHATSAPP_FROM = os.environ.get("TWILIO_WHATSAPP_FROM",
                  os.environ.get("TWILIO_WHATSAPP_NUMBER", ""))
PUBLIC_BASE   = os.environ.get("AIOS_PUBLIC_BASE",
                  os.environ.get("AIOS_UI_BASE", "https://aios.grupojdl.pt")).rstrip("/")

# ── DB ────────────────────────────────────────────────────────────────────────

def db():
    return psycopg2.connect(DSN, cursor_factory=RealDictCursor)

def q1(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return dict(rows[0]) if rows else {}

def qa(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

def ex(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)

# ── WhatsApp send (isolado, sem dependência do whatsapp_handler) ──────────────

def _send_whatsapp(to_phone: str, body: str) -> str | None:
    if not (ACCOUNT_SID and AUTH_TOKEN and WHATSAPP_FROM):
        print(json.dumps({"event": "marketplace_invite_skipped",
                          "reason": "twilio_not_configured", "to": to_phone}))
        return None
    to_wa   = f"whatsapp:{to_phone}" if not to_phone.startswith("whatsapp:") else to_phone
    from_wa = f"whatsapp:{WHATSAPP_FROM}" if not WHATSAPP_FROM.startswith("whatsapp:") else WHATSAPP_FROM
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json",
            auth=(ACCOUNT_SID, AUTH_TOKEN),
            data={"From": from_wa, "To": to_wa, "Body": body},
            timeout=10,
        )
        data = r.json()
        return data.get("sid") or None
    except Exception as e:
        print(json.dumps({"event": "marketplace_invite_error", "to": to_phone, "error": str(e)}))
        return None

# ── Mensagem de convite ───────────────────────────────────────────────────────

def _build_invite_msg(job: dict) -> str:
    starts = job.get("starts_at")
    if hasattr(starts, "strftime"):
        date_str = starts.strftime("%d/%m/%Y %H:%M")
    else:
        date_str = str(starts)[:16].replace("T", " ")
    loc   = job.get("location") or ""
    title = job.get("title", "Serviço")
    resp  = f"{PUBLIC_BASE}/marketplace/respond" if PUBLIC_BASE else ""
    lines = [
        f"Olá! Tens disponibilidade para um serviço?",
        f"",
        f"📋 *{title}*",
        f"📅 {date_str}",
    ]
    if loc:
        lines.append(f"📍 {loc}")
    lines += [
        f"",
        f"Responde com:",
        f"  *SIM* — estou disponível",
        f"  *NAO* — não consigo",
    ]
    return "\n".join(lines)

# ── Core ─────────────────────────────────────────────────────────────────────

def invite(job_id: int, worker_id: int | None = None):
    conn = db()
    try:
        job = q1(conn, """
            SELECT mj.id, mj.title, mj.location, mj.starts_at, mj.ends_at, mj.status,
                   c.name AS client_name
            FROM public.marketplace_jobs mj
            LEFT JOIN public.clients c ON c.id = mj.client_id
            WHERE mj.id = %s
        """, (job_id,))
        if not job:
            print(json.dumps({"event": "marketplace_invite_error",
                              "job_id": job_id, "error": "job_not_found"}))
            return

        if job["status"] not in ("open",):
            print(json.dumps({"event": "marketplace_invite_skipped",
                              "job_id": job_id, "reason": f"status={job['status']}"}))
            return

        # Seleccionar applications a convidar
        if worker_id:
            apps = qa(conn, """
                SELECT ma.id, ma.worker_id, ma.status,
                       p.name AS worker_name,
                       mwp.whatsapp_phone
                FROM public.marketplace_applications ma
                JOIN public.persons p ON p.id = ma.worker_id
                LEFT JOIN public.marketplace_worker_profiles mwp ON mwp.worker_id = ma.worker_id
                WHERE ma.job_id = %s AND ma.worker_id = %s
            """, (job_id, worker_id))
        else:
            apps = qa(conn, """
                SELECT ma.id, ma.worker_id, ma.status,
                       p.name AS worker_name,
                       mwp.whatsapp_phone
                FROM public.marketplace_applications ma
                JOIN public.persons p ON p.id = ma.worker_id
                LEFT JOIN public.marketplace_worker_profiles mwp ON mwp.worker_id = ma.worker_id
                WHERE ma.job_id = %s AND ma.status = 'invited'
            """, (job_id,))

        msg     = _build_invite_msg(job)
        sent    = 0
        skipped = 0

        for app in apps:
            phone = (app.get("whatsapp_phone") or "").strip()
            if not phone:
                print(json.dumps({"event": "marketplace_invite_skipped",
                                  "app_id": app["id"], "worker": app["worker_name"],
                                  "reason": "no_whatsapp_phone"}))
                skipped += 1
                continue

            sid = _send_whatsapp(phone, msg)
            if sid:
                print(json.dumps({"event": "marketplace_invites_sent",
                                  "app_id": app["id"], "worker": app["worker_name"],
                                  "job_id": job_id, "sid": sid}))
                sent += 1
            else:
                skipped += 1

        conn.commit()
        print(json.dumps({"event": "marketplace_invite_batch_done",
                          "job_id": job_id, "sent": sent, "skipped": skipped}))
    except Exception as e:
        print(json.dumps({"event": "marketplace_invite_error",
                          "job_id": job_id, "error": str(e)}))
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-id", type=int, required=True)
    parser.add_argument("--worker-id", type=int, default=None)
    args = parser.parse_args()
    invite(args.job_id, args.worker_id)
