#!/usr/bin/env python3
"""
whatsapp_fallback.py — Fallback de email quando WhatsApp falha com 63016

Chamado pelo server.js quando o status webhook recebe ErrorCode=63016.
Procura o timesheet pelo client_message_sid, envia email ao cliente,
marca delivery_status e loga evento estruturado.

Uso:
  python3 bin/whatsapp_fallback.py --sid <MessageSid> --to <phone>
Output: JSON {"event": "...", "result": "..."}
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, datetime as dt, json, os, sys

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from email_send import send_email as _send_email
except ImportError:
    _send_email = None

# ── Env ───────────────────────────────────────────────────────────────────────
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip()

DSN         = os.environ.get("DATABASE_URL", "dbname=aios user=aios_user password=jdl host=127.0.0.1")
PUBLIC_BASE = os.environ.get("AIOS_PUBLIC_BASE",
                os.environ.get("AIOS_UI_BASE", "https://aios.grupojdl.pt")).rstrip("/")

# ── DB helpers ────────────────────────────────────────────────────────────────

def db():
    return psycopg2.connect(DSN, cursor_factory=RealDictCursor)

def q1(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return dict(rows[0]) if rows else {}

def ex(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)

def _tz(ts):
    if ts is None:
        return None
    if not hasattr(ts, "tzinfo") or ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts

def _fmtt(ts) -> str:
    ts = _tz(ts)
    return ts.strftime("%H:%M") if ts else "?"

# ── Resolução de destinatário ─────────────────────────────────────────────────

def resolve_client_context(conn, ts_id: int) -> dict:
    """
    Resolve client_id para um timesheet, mesmo quando client_id é NULL.

    Ordem:
      1. event_timesheets.client_id  (direto)
      2. worker_contacts.default_client_phone → client_contacts.phone
      3. event_timesheets.client_phone → client_contacts.phone
      4. worker_contacts.default_client_email → client_contacts.email
      5. não resolvido

    Returns: {"client_id": int|None, "contact_id": int|None, "source": str}
    """
    ts = q1(conn, """
        SELECT client_id, worker_phone, client_phone
        FROM public.event_timesheets WHERE id = %s
    """, (ts_id,))
    if not ts:
        return {"client_id": None, "contact_id": None, "source": "ts_not_found"}

    if ts.get("client_id"):
        return {"client_id": ts["client_id"], "contact_id": None,
                "source": "timesheet_direct"}

    worker_phone = (ts.get("worker_phone") or "").strip()
    client_phone = (ts.get("client_phone") or "").strip()
    wc = {}
    if worker_phone:
        wc = q1(conn, """
            SELECT default_client_phone, default_client_email
            FROM public.worker_contacts
            WHERE whatsapp_phone = %s AND active = true LIMIT 1
        """, (worker_phone,))

    for lookup, src in [
        ((wc.get("default_client_phone") or "").strip(), "worker_default_client_phone"),
        (client_phone,                                    "timesheet_client_phone"),
    ]:
        if lookup:
            cc = q1(conn, """
                SELECT id, client_id FROM public.client_contacts
                WHERE phone = %s LIMIT 1
            """, (lookup,))
            if cc.get("client_id"):
                return {"client_id": cc["client_id"], "contact_id": cc["id"], "source": src}

    lookup_email = (wc.get("default_client_email") or "").strip()
    if lookup_email:
        cc = q1(conn, """
            SELECT id, client_id FROM public.client_contacts
            WHERE email = %s LIMIT 1
        """, (lookup_email,))
        if cc.get("client_id"):
            return {"client_id": cc["client_id"], "contact_id": cc["id"],
                    "source": "worker_default_client_email"}

    return {"client_id": None, "contact_id": None, "source": "unresolved"}


def _resolve_email_recipient(conn, ts_id: int,
                             worker_phone: str = "") -> tuple[str, str]:
    """
    Resolve destinatário de email. Chama resolve_client_context() para garantir
    que client_id é resolvido mesmo quando event_timesheets.client_id é NULL.
    Emite client_context_resolved + email_recipient_resolved via print/JSON.

    Prioridade de email:
      1. client_contacts role='accounting' AND can_receive_email
      2. client_contacts is_primary=true   AND can_receive_email
      3. client_contacts qualquer          AND can_receive_email
      4. clients.contact_email
      5. worker_contacts.default_client_email (legacy)

    Returns: (email, source)
    """
    ctx = resolve_client_context(conn, ts_id)
    client_id = ctx["client_id"]
    print(json.dumps({
        "event": "client_context_resolved",
        "timesheet_id": ts_id,
        "client_id": client_id,
        "source": ctx["source"],
    }))

    email, source = "", "none"

    if client_id:
        for where, esrc in [
            ("role='accounting' AND can_receive_email=true", "client_contacts_accounting"),
            ("is_primary=true AND can_receive_email=true",   "client_contacts_primary"),
            ("can_receive_email=true",                       "client_contacts_any"),
        ]:
            row = q1(conn, f"""
                SELECT email FROM public.client_contacts
                WHERE client_id=%s AND {where}
                  AND email IS NOT NULL AND email <> ''
                ORDER BY id LIMIT 1
            """, (client_id,))
            if row.get("email"):
                email, source = row["email"], esrc
                break

        if not email:
            cl = q1(conn, """
                SELECT contact_email FROM public.clients
                WHERE id=%s AND contact_email IS NOT NULL AND contact_email <> ''
            """, (client_id,))
            if cl.get("contact_email"):
                email, source = cl["contact_email"], "clients_contact_email"

    if not email and worker_phone:
        wc = q1(conn, """
            SELECT default_client_email FROM public.worker_contacts
            WHERE whatsapp_phone=%s AND active=true
              AND default_client_email IS NOT NULL AND default_client_email <> ''
            LIMIT 1
        """, (worker_phone,))
        if wc.get("default_client_email"):
            email, source = wc["default_client_email"], "worker_contacts_legacy"

    print(json.dumps({
        "event": "email_recipient_resolved",
        "timesheet_id": ts_id,
        "client_id": client_id,
        "recipient_email": email,
        "source": source,
    }))
    return email, source


# ── Fallback ──────────────────────────────────────────────────────────────────

def run(sid: str, to_phone: str):
    conn = db()
    try:
        # Procurar timesheet pelo SID guardado no checkout
        row = q1(conn, """
            SELECT et.*,
                   wc.client_name
            FROM public.event_timesheets et
            LEFT JOIN public.worker_contacts wc
                   ON wc.whatsapp_phone = et.worker_phone AND wc.active = TRUE
            WHERE et.client_message_sid = %s
        """, (sid,))

        if not row:
            print(json.dumps({
                "event": "whatsapp_template_required",
                "sid": sid, "to": to_phone, "result": "sid_not_found",
            }))
            return

        # Evitar processar duas vezes
        if row.get("delivery_status") in ("whatsapp_63016", "email_fallback"):
            print(json.dumps({
                "event": "whatsapp_template_required",
                "sid": sid, "result": "already_handled",
            }))
            return

        # Marcar delivery_status
        ex(conn, """
            UPDATE public.event_timesheets
            SET delivery_status = 'whatsapp_63016', updated_at = now()
            WHERE client_message_sid = %s
        """, (sid,))
        conn.commit()

        # Log evento estruturado
        print(json.dumps({
            "event":        "whatsapp_template_required",
            "sid":          sid,
            "to":           to_phone,
            "timesheet_id": row.get("id"),
            "worker":       row.get("worker_id"),
        }))

        # Resolver destinatário via client_contacts (com fallback estrutural)
        ts_id = row.get("id")
        worker_phone = (row.get("worker_phone") or "").strip()
        email, email_source = _resolve_email_recipient(conn, ts_id, worker_phone)
        if not email:
            print(json.dumps({"event": "whatsapp_template_required",
                              "sid": sid, "result": "no_email_channel"}))
            return

        if _send_email is None:
            print(json.dumps({"event": "whatsapp_template_required",
                              "sid": sid, "result": "email_module_unavailable"}))
            return

        # Reconstruir conteúdo do email
        token    = row.get("validation_token", "")
        val_url  = f"{PUBLIC_BASE}/validar/{token}" if token else ""
        worker   = row.get("worker_id", "Colaborador")
        log_date = row.get("log_date")
        date_str = log_date.strftime("%d/%m/%Y") if hasattr(log_date, "strftime") else str(log_date)[:10]
        hours    = float(row.get("hours") or 0)
        total    = float(row.get("invoice_total") or 0)
        location = row.get("location") or ""
        notes    = (row.get("notes") or "").strip()
        start_str = _fmtt(row.get("start_time"))
        end_str   = _fmtt(row.get("check_out_at"))
        client_name = (row.get("client_name") or "").strip()
        greeting    = f"<p>Olá {client_name},</p>" if client_name else ""

        note_html = ""
        if notes:
            lines = [l.strip() for l in notes.splitlines() if l.strip()]
            items = "".join(f"<li style='margin:.2rem 0'>{l}</li>" for l in lines)
            note_html = (
                f'<div style="background:#f0fdf4;border-left:4px solid #16a34a;'
                f'padding:.75rem 1rem;margin:1rem 0;border-radius:4px">'
                f'<b style="color:#15803d">Notas do colaborador:</b>'
                f'<ul style="margin:.5rem 0;padding-left:1.5rem">{items}</ul></div>'
            )

        validate_btn = (
            f'<p style="margin:2rem 0">'
            f'<a href="{val_url}" style="background:#16a34a;color:#fff;padding:.75rem 1.5rem;'
            f'border-radius:6px;text-decoration:none;font-weight:bold">Validar serviço</a></p>'
        ) if val_url else ""

        email_html = f"""<html><body style="font-family:sans-serif;color:#1e293b;max-width:600px;margin:auto">
<p style="color:#64748b;font-size:.85em;margin-bottom:.25rem">Ao cuidado de Ana Pereira</p>
<h2 style="color:#0f172a">Serviço registado — validação pendente</h2>
{greeting}
<table style="width:100%;border-collapse:collapse;margin:1rem 0">
  <tr><td style="padding:.4rem 0;color:#64748b;width:140px"><b>Colaborador</b></td><td>{worker}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Data</b></td><td>{date_str}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Horário</b></td><td>{start_str} → {end_str} ({hours:.1f}h)</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Local</b></td><td>{location}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Valor (c/IVA)</b></td><td><b style="font-size:1.2em">{total:.2f}€</b></td></tr>
</table>
{note_html}
{validate_btn}
<p style="color:#94a3b8;font-size:.85em">Grupo JDL · joaodiogo@grupojdl.pt</p>
</body></html>"""

        note_subj = f" — {notes.splitlines()[0]}" if notes else ""
        res = _send_email(
            email,
            f"Serviço {date_str} — {worker}{note_subj} — validação",
            email_html,
        )
        ok = res.get("ok", False) if isinstance(res, dict) else bool(res)

        if ok:
            ex(conn, """
                UPDATE public.event_timesheets
                SET delivery_status = 'email_fallback', updated_at = now()
                WHERE client_message_sid = %s
            """, (sid,))
            conn.commit()

        print(json.dumps({
            "event":  "whatsapp_template_required",
            "sid":    sid,
            "result": "email_sent" if ok else "email_failed",
        }))

    except Exception as e:
        print(json.dumps({"event": "whatsapp_fallback_error", "sid": sid, "error": str(e)}))
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sid", required=True)
    parser.add_argument("--to", default="")
    args = parser.parse_args()
    run(args.sid, args.to)
