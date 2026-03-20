#!/usr/bin/env python3
"""
whatsapp_handler.py — Ponto WhatsApp via Twilio

Fluxo obrigatório (texto + GPS, ambos necessários):

  CHECK-IN:
    1. Worker envia: inicio / cheguei / entrar / start ...
    2. Sistema: "Envia localização"  → status=pending_gps_in
    3. Worker partilha localização GPS (≤10 min)
    4. Sistema: "Entrada registada ✅"  → status=active

  CHECK-OUT:
    1. Worker envia: fim / sair / terminei / acabei ...
    2. Sistema: "Envia localização"  → status=pending_gps_out
    3. Worker partilha localização GPS (≤10 min)
    4. Sistema: "Saída registada ✅" + envia ao cliente  → status=submitted

Status possíveis de event_timesheets:
  pending_gps_in  → comando recebido, aguarda GPS check-in
  active          → check-in confirmado com GPS
  pending_gps_out → fim recebido, aguarda GPS check-out
  submitted       → check-out confirmado, enviado ao cliente
  approved        → validado pelo cliente
  rejected        → rejeitado pelo cliente

Chamado pelo server.js:
  python3 bin/whatsapp_handler.py --from +351XXXX --body "inicio" [--lat L --lon L] [--sid SM...]
Output: JSON {"reply": "...", "ok": true/false}
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, datetime as dt, json, logging, os, requests, unicodedata, uuid

import psycopg2
from psycopg2.extras import RealDictCursor

try:
    from email_send import send_email as _send_email
except ImportError:
    _send_email = None

# ── Config ────────────────────────────────────────────────────────────────────

DSN            = os.environ.get("DATABASE_URL", "dbname=aios user=aios_user password=jdl host=127.0.0.1")
ACCOUNT_SID    = os.environ.get("TWILIO_ACCOUNT_SID", "")
AUTH_TOKEN     = os.environ.get("TWILIO_AUTH_TOKEN", "")
WHATSAPP_FROM  = os.environ.get("TWILIO_WHATSAPP_FROM",
                   os.environ.get("TWILIO_WHATSAPP_NUMBER", ""))
WHATSAPP_MODE  = os.environ.get("WHATSAPP_MODE", "sandbox").lower()
STATUS_CB_URL  = os.environ.get("TWILIO_STATUS_CALLBACK_URL", "")
DEFAULT_CLIENT = os.environ.get("DEFAULT_CLIENT_WHATSAPP", "")
PUBLIC_BASE    = os.environ.get("AIOS_PUBLIC_BASE",
                   os.environ.get("AIOS_UI_BASE", "https://aios.grupojdl.pt")).rstrip("/")
TG_TOKEN       = os.environ.get("AIOS_TG_TOKEN", "")
TG_CHAT        = os.environ.get("AIOS_TG_CHAT", "")

# Templates WhatsApp (preenchidos após aprovação no Twilio Content Builder)
WA_TMPL_CHECKIN   = os.environ.get("WHATSAPP_TEMPLATE_CHECKIN_PT",  "")
WA_TMPL_CHECKOUT  = os.environ.get("WHATSAPP_TEMPLATE_CHECKOUT_PT", "")
MESSAGING_SVC_SID = os.environ.get("TWILIO_MESSAGING_SERVICE_SID",  "")

GPS_WINDOW_MIN = 10   # janela máxima entre comando e GPS (minutos)

# Números cujo inbound gera alerta Telegram imediato (vírgula-separados)
_watched_raw = os.environ.get("WATCHED_WHATSAPP_NUMBERS", "")
WATCHED_NUMBERS: set[str] = {n.strip() for n in _watched_raw.split(",") if n.strip()}

AIOS_ROOT = os.path.dirname(_bin_dir)

# ── Logging ───────────────────────────────────────────────────────────────────

_log_dir = os.path.join(AIOS_ROOT, "runtime", "whatsapp")
os.makedirs(_log_dir, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(_log_dir, "handler.log"),
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

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

def _now_utc():
    return dt.datetime.now(dt.timezone.utc)

def _tz(ts):
    if ts is None:
        return None
    if not hasattr(ts, "tzinfo") or ts.tzinfo is None:
        return ts.replace(tzinfo=dt.timezone.utc)
    return ts

# ── Phone normalisation ───────────────────────────────────────────────────────

def _variants(phone: str):
    clean = phone.lstrip("+")
    return (phone, "+" + clean, clean)

def _first(name: str) -> str:
    return (name or "").split()[0] if name else ""

# ── Worker lookup ─────────────────────────────────────────────────────────────

def lookup_worker(conn, phone: str) -> dict:
    v = _variants(phone)
    return q1(conn, """
        SELECT wc.id, wc.worker_name, wc.people_id, wc.default_client_phone,
               wc.default_client_email, wc.cc_phone, wc.client_name, p.name AS person_name
        FROM public.worker_contacts wc
        LEFT JOIN public.persons p ON p.id = wc.people_id
        WHERE wc.active = TRUE
          AND wc.whatsapp_phone IN (%s, %s, %s)
        LIMIT 1
    """, v)

# ── Shift queries ─────────────────────────────────────────────────────────────

def get_shift(conn, phone: str, status: str) -> dict:
    v = _variants(phone)
    return q1(conn, """
        SELECT * FROM public.event_timesheets
        WHERE worker_phone IN (%s, %s, %s) AND status = %s
        ORDER BY updated_at DESC LIMIT 1
    """, (*v, status))

def get_open_shift(conn, phone: str) -> dict:
    """Qualquer turno em curso (pending_in, active, pending_out)."""
    v = _variants(phone)
    return q1(conn, """
        SELECT * FROM public.event_timesheets
        WHERE worker_phone IN (%s, %s, %s)
          AND status IN ('pending_gps_in','active','pending_gps_out')
        ORDER BY updated_at DESC LIMIT 1
    """, v)

# ── Twilio send ───────────────────────────────────────────────────────────────

def send_whatsapp_message(to_phone: str, body: str) -> str | None:
    """Enviar WhatsApp. Agnóstico ao número (sandbox ou produção). Retorna SID."""
    if not (ACCOUNT_SID and AUTH_TOKEN and WHATSAPP_FROM):
        log.warning("Twilio não configurado — mensagem não enviada para %s", to_phone)
        return None
    to = to_phone if to_phone.startswith("+") else "+" + to_phone.lstrip("+")
    payload = {"From": f"whatsapp:{WHATSAPP_FROM}", "To": f"whatsapp:{to}", "Body": body}
    if STATUS_CB_URL:
        payload["StatusCallback"] = STATUS_CB_URL
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json",
            auth=(ACCOUNT_SID, AUTH_TOKEN), data=payload, timeout=10,
        )
        r.raise_for_status()
        sid = r.json().get("sid", "")
        log.info("SEND to=%s mode=%s sid=%s", to, WHATSAPP_MODE, sid)
        return sid
    except Exception as e:
        log.error("SEND ERROR to=%s: %s", to, e)
        return None

def _send_telegram(text: str) -> bool:
    """Notificação Telegram — fallback para quando WhatsApp outbound falha (63051)."""
    if not (TG_TOKEN and TG_CHAT):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        ok = r.status_code == 200
        log.info("TELEGRAM notify ok=%s", ok)
        return ok
    except Exception as e:
        log.warning("TELEGRAM ERROR: %s", e)
        return False


# ── Janela 24h / envio híbrido ────────────────────────────────────────────────

def update_inbound_window(conn, phone: str):
    """Regista/actualiza janela de inbound para este número."""
    ex(conn, """
        INSERT INTO public.whatsapp_sessions(phone, last_inbound_at, updated_at)
        VALUES(%s, now(), now())
        ON CONFLICT (phone) DO UPDATE
          SET last_inbound_at = now(), updated_at = now()
    """, (phone,))
    conn.commit()


def is_within_24h(conn, phone: str) -> bool:
    """True se o número enviou mensagem inbound nas últimas 24h."""
    for p in _variants(phone):
        r = q1(conn, "SELECT last_inbound_at FROM public.whatsapp_sessions WHERE phone=%s", (p,))
        if r:
            ts = _tz(r["last_inbound_at"])
            return (_now_utc() - ts).total_seconds() <= 86400
    return False


def send_whatsapp_template(to_phone: str, content_sid: str, variables: dict) -> str | None:
    """Envia template aprovado via ContentSid/ContentVariables (fora da janela 24h)."""
    if not (ACCOUNT_SID and AUTH_TOKEN and content_sid):
        log.warning("send_whatsapp_template: não configurado para %s (content_sid vazio)", to_phone)
        return None
    to = to_phone if to_phone.startswith("+") else "+" + to_phone.lstrip("+")
    payload = {
        "To":              f"whatsapp:{to}",
        "From":            f"whatsapp:{WHATSAPP_FROM}",
        "ContentSid":      content_sid,
        "ContentVariables": json.dumps(variables, ensure_ascii=False),
    }
    if STATUS_CB_URL:
        payload["StatusCallback"] = STATUS_CB_URL
    try:
        r = requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{ACCOUNT_SID}/Messages.json",
            auth=(ACCOUNT_SID, AUTH_TOKEN), data=payload, timeout=10,
        )
        r.raise_for_status()
        sid = r.json().get("sid", "")
        log.info("whatsapp_template_message_sent to=%s sid=%s tmpl=%s", to, sid, content_sid)
        return sid
    except Exception as e:
        log.error("whatsapp_template_message_sent ERROR to=%s: %s", to, e)
        return None


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
    Resolve destinatário de email para notificações de checkout/fallback.
    Chama resolve_client_context() para garantir que client_id é resolvido
    mesmo quando event_timesheets.client_id é NULL.

    Emite logs estruturados: client_context_resolved + email_recipient_resolved.

    Prioridade de email:
      1. client_contacts  role='accounting'  AND can_receive_email
      2. client_contacts  is_primary=true    AND can_receive_email
      3. client_contacts  qualquer           AND can_receive_email
      4. clients.contact_email
      5. worker_contacts.default_client_email  (legacy)

    Returns: (email, source)
    """
    ctx = resolve_client_context(conn, ts_id)
    client_id = ctx["client_id"]
    log.info("client_context_resolved %s", json.dumps({
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

    log.info("email_recipient_resolved %s", json.dumps({
        "event": "email_recipient_resolved",
        "timesheet_id": ts_id,
        "client_id": client_id,
        "recipient_email": email,
        "source": source,
    }))
    return email, source


def _email_fallback_sync(conn, ts_id: int) -> bool:
    """
    Envia email de fallback quando o WhatsApp falha (template não aprovado, fora da janela).
    Busca dados do timesheet e email do cliente directamente.
    Retorna True se enviado com sucesso.
    """
    if not _send_email:
        log.warning("email_fallback_skipped ts_id=%s: email_send not available", ts_id)
        return False
    try:
        row = q1(conn, """
            SELECT t.id, t.validation_token, t.invoice_total,
                   t.worker_phone, t.start_time, t.check_out_at,
                   w.worker_name, w.client_name
            FROM public.event_timesheets t
            LEFT JOIN public.worker_contacts w ON w.whatsapp_phone = t.worker_phone
            WHERE t.id = %s
        """, (ts_id,))
        if not row:
            log.warning("email_fallback_skipped ts_id=%s: timesheet not found", ts_id)
            return False
        client_email, email_source = _resolve_email_recipient(
            conn, ts_id, row.get("worker_phone", ""))
        if not client_email:
            log.warning("email_fallback_skipped ts_id=%s: no recipient resolved", ts_id)
            return False
        token = row.get("validation_token", "")
        val_url = f"{PUBLIC_BASE}/validar/{token}" if token else PUBLIC_BASE
        worker_name  = row.get("worker_name") or row.get("worker_phone", "")
        client_name  = (row.get("client_name") or "").strip()
        greeting     = f"<p>Olá {client_name},</p>" if client_name else ""
        total        = float(row.get("invoice_total") or 0)
        checkin_dt   = row.get("start_time")
        checkout_dt  = row.get("check_out_at")
        date_str     = checkin_dt.strftime("%d/%m/%Y") if checkin_dt else "—"
        start_str    = checkin_dt.strftime("%H:%M") if checkin_dt else "—"
        end_str      = checkout_dt.strftime("%H:%M") if checkout_dt else "—"
        hours        = ((checkout_dt - checkin_dt).total_seconds() / 3600) if (checkin_dt and checkout_dt) else 0

        email_html = f"""
<html><body style="font-family:sans-serif;color:#1e293b;max-width:600px;margin:auto">
<p style="color:#64748b;font-size:.85em;margin-bottom:.25rem">Ao cuidado de Ana Pereira</p>
<h2 style="color:#0f172a">Serviço registado — validação pendente</h2>
{greeting}
<table style="width:100%;border-collapse:collapse;margin:1rem 0">
  <tr><td style="padding:.4rem 0;color:#64748b;width:140px"><b>Colaborador</b></td><td>{worker_name}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Data</b></td><td>{date_str}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Horário</b></td><td>{start_str} → {end_str} ({hours:.1f}h)</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Valor (c/IVA)</b></td><td><b style="font-size:1.2em">{total:.2f}€</b></td></tr>
</table>
<p style="margin:2rem 0">
  <a href="{val_url}" style="background:#16a34a;color:#fff;padding:.75rem 1.5rem;border-radius:6px;text-decoration:none;font-weight:bold">
    Validar serviço
  </a>
</p>
<p style="color:#94a3b8;font-size:.85em">Grupo JDL · joaodiogo@grupojdl.pt</p>
</body></html>"""
        subj = f"Serviço {date_str} — {worker_name} — validação pendente"
        res = _send_email(client_email, subj, email_html)
        ok = res.get("ok") if isinstance(res, dict) else bool(res)
        log.info("email_fallback_sent ts_id=%s to=%s ok=%s", ts_id, client_email, ok)
        return bool(ok)
    except Exception as e:
        log.error("email_fallback_error ts_id=%s: %s", ts_id, e)
        return False


def send_client_message(conn, to_phone: str, body: str,
                        content_sid: str = "", variables: dict = None,
                        ts_id: int = None) -> str | None:
    """
    Envio híbrido para clientes/CC:
    - Dentro da janela 24h  → free-form (send_whatsapp_message)
    - Fora da janela        → template (send_whatsapp_template)
    - Template falha / sem template → email fallback se ts_id fornecido
    Fallback assíncrono de 63016 é tratado em whatsapp_fallback.py via status webhook.
    """
    variables = variables or {}
    if is_within_24h(conn, to_phone):
        sid = send_whatsapp_message(to_phone, body)
        if sid:
            log.info("whatsapp_session_message_sent to=%s", to_phone)
        return sid
    # Fora da janela → template
    if content_sid:
        sid = send_whatsapp_template(to_phone, content_sid, variables)
        if sid:
            return sid
        # Template falhou (não aprovado, 21703, etc.) → email fallback
        log.warning("whatsapp_template_failed to=%s tmpl=%s → email fallback", to_phone, content_sid)
        if ts_id:
            _email_fallback_sync(conn, ts_id)
        return None
    # Sem template → email fallback
    log.warning("whatsapp_template_required to=%s (sem template configurado)", to_phone)
    if ts_id:
        _email_fallback_sync(conn, ts_id)
    return None


# ── Reverse geocode ───────────────────────────────────────────────────────────

def reverse_geocode(lat: float, lon: float) -> str:
    """Converte coordenadas em morada legível via Nominatim (OSM). Fallback = coords."""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json", "addressdetails": 1},
            headers={"User-Agent": "aios-grupojdl/1.0"},
            timeout=5,
        )
        r.raise_for_status()
        a = r.json().get("address", {})
        parts = []
        road = a.get("road") or a.get("pedestrian") or a.get("path")
        if road:
            num = a.get("house_number", "")
            parts.append(f"{road}{', ' + num if num else ''}")
        city = a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
        if city:
            parts.append(city)
        result = " · ".join(parts) if parts else r.json().get("display_name", "")
        return result or f"{lat:.5f}, {lon:.5f}"
    except Exception as e:
        log.warning("reverse_geocode failed lat=%s lon=%s: %s", lat, lon, e)
        return f"{lat:.5f}, {lon:.5f}"


# ── Billing ───────────────────────────────────────────────────────────────────

def _get_rate(conn) -> dict:
    r = q1(conn, "SELECT * FROM public.service_rates WHERE active=TRUE AND name='standard' LIMIT 1")
    return r or {"rate_client_day": 100.0, "rate_worker_day": 50.0,
                 "car_bonus_day": 10.0, "vat_rate": 23.0}

def _calc(hours: float, car_used: bool, rate: dict) -> dict:
    days = 2.0 if hours > 16 else (1.5 if hours > 12 else 1.0)
    pay  = float(rate["rate_worker_day"]) * days + (float(rate.get("car_bonus_day", 10)) * days if car_used else 0)
    net  = float(rate["rate_client_day"]) * days
    vat  = net * float(rate["vat_rate"]) / 100
    return {"days": days, "worker_pay": round(pay, 2),
            "invoice_net": round(net, 2), "invoice_vat": round(vat, 2),
            "invoice_total": round(net + vat, 2)}

# ── Check-in request (texto) ──────────────────────────────────────────────────

def handle_inicio_request(conn, phone: str, worker: dict,
                          car_used: bool = False, note: str = "") -> str:
    """Comando recebido — criar pending_gps_in e pedir GPS."""
    existing = get_open_shift(conn, phone)
    if existing:
        s = existing["status"]
        if s == "pending_gps_in":
            return "⏳ Aguardo a tua localização para confirmar a entrada.\nPartilha a posição no WhatsApp."
        sin  = _tz(existing["start_time"])
        sstr = sin.strftime("%H:%M") if sin else "?"
        if s == "pending_gps_out":
            return f"⏳ Aguardo localização para fechar o turno das *{sstr}*.\nEnvia *fim* para voltar a pedir."
        return f"⚠️ Já tens entrada aberta às *{sstr}*.\nManda *fim* para fechar."

    people_id   = worker.get("people_id")
    worker_name = worker.get("person_name") or worker.get("worker_name", "Colaborador")

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO public.event_timesheets
              (worker_id, worker_phone, event_name, start_time, status,
               log_date, people_id, car_used, notes)
            VALUES (%s, %s, %s, NOW(), 'pending_gps_in',
                    CURRENT_DATE, %s, %s, %s)
            RETURNING id
        """, (worker_name, phone, f"Turno {dt.date.today().isoformat()}",
              people_id, car_used, note or None))
    conn.commit()
    extras = ""
    if note:
        extras += f"\n📝 *{note}*"
    if car_used:
        extras += "\n🚗 Carro registado (+10€/dia)."
    log.info("PENDING_GPS_IN phone=%s worker=%s car=%s note=%r", phone, worker_name, car_used, note)
    return f"📍 Envia a tua *localização* para confirmar a entrada.{extras}"

# ── Check-out request (texto) ─────────────────────────────────────────────────

def handle_fim_request(conn, phone: str) -> str:
    """Comando 'fim' recebido — marcar pending_gps_out e pedir GPS."""
    shift = get_shift(conn, phone, "active")
    if not shift:
        if get_shift(conn, phone, "pending_gps_out"):
            return "⏳ Aguardo a tua localização para confirmar a saída.\nPartilha a posição no WhatsApp."
        if get_shift(conn, phone, "pending_gps_in"):
            return "⏳ Ainda não confirmaste a entrada. Partilha a localização primeiro."
        return "Sem turno aberto. Manda *inicio* para começar."

    ex(conn, """
        UPDATE public.event_timesheets
        SET status='pending_gps_out', check_out_at=NOW(), updated_at=NOW()
        WHERE id=%s
    """, (shift["id"],))
    conn.commit()
    log.info("PENDING_GPS_OUT phone=%s shift_id=%s", phone, shift["id"])
    return "📍 Envia a tua *localização* para confirmar a saída."

# ── GPS confirm (o coração do sistema) ───────────────────────────────────────

def handle_gps_confirm(conn, phone: str, lat: str, lon: str, addr: str = "") -> str:
    """GPS recebido — confirmar check-in ou check-out pending."""
    lat_f = float(lat)
    lon_f = float(lon)
    now   = _now_utc()
    location_str = addr or reverse_geocode(lat_f, lon_f)

    # ── 1. Confirmar check-in ────────────────────────────────────────────────
    pending_in = get_shift(conn, phone, "pending_gps_in")
    if pending_in:
        start   = _tz(pending_in["start_time"])
        elapsed = (now - start).total_seconds() / 60

        if elapsed > GPS_WINDOW_MIN:
            ex(conn, "DELETE FROM public.event_timesheets WHERE id=%s", (pending_in["id"],))
            conn.commit()
            log.warning("GPS_EXPIRED check-in phone=%s elapsed=%.1fm", phone, elapsed)
            return (
                f"⏱ Tempo expirado ({GPS_WINDOW_MIN} min).\n"
                f"Envia *inicio* novamente e partilha a localização de seguida."
            )

        sstr = start.strftime("%H:%M")
        ex(conn, """
            UPDATE public.event_timesheets
            SET status='active', gps_lat=%s, gps_lon=%s,
                gps_source='whatsapp_gps', location=%s, updated_at=NOW()
            WHERE id=%s
        """, (lat_f, lon_f, location_str, pending_in["id"]))
        conn.commit()
        log.info("CHECK-IN CONFIRMED phone=%s loc=%s", phone, location_str)

        # Notificar cliente da chegada do colaborador
        worker_name_in = pending_in.get("worker_id", "Colaborador")
        service_note_in = (pending_in.get("notes") or "").strip()
        worker_obj_in   = lookup_worker(conn, phone)
        client_phone_in = (worker_obj_in.get("default_client_phone") or DEFAULT_CLIENT or "").strip()
        client_name_in = (worker_obj_in.get("client_name") or "").strip()
        if client_phone_in:
            note_cl = f"\n📝 {service_note_in}" if service_note_in else ""
            cli_greeting = f"Olá {client_name_in}! " if client_name_in else ""
            checkin_msg = (
                f"{cli_greeting}🟢 *{worker_name_in} chegou*\n"
                f"📍 {location_str}"
                f"{note_cl}"
            )
            send_client_message(
                conn, client_phone_in, checkin_msg,
                content_sid=WA_TMPL_CHECKIN,
                variables={"1": client_name_in or "Cliente",
                           "2": worker_name_in,
                           "3": location_str},
            )
            log.info("NOTIF CLIENTE CHECKIN %s worker=%s", client_phone_in, worker_name_in)
        cc_in = (worker_obj_in.get("cc_phone") or "").strip()
        if cc_in and cc_in != client_phone_in:
            note_cl = f"\n📝 {service_note_in}" if service_note_in else ""
            send_client_message(conn, cc_in,
                                f"🟢 *{worker_name_in} chegou*\n📍 {location_str}{note_cl}")
            log.info("NOTIF CC CHECKIN %s worker=%s", cc_in, worker_name_in)

        first = _first(worker_obj_in.get("worker_name", ""))
        greeting = f"Olá {first}! " if first else ""
        note_line = f"\n📝 {pending_in['notes']}" if pending_in.get("notes") else ""
        return (
            f"{greeting}✅ Entrada registada: *{sstr}*\n"
            f"📍 {location_str}"
            f"{note_line}\n"
            f"Bom trabalho! 💪 Manda *fim* quando terminares."
        )

    # ── 2. Confirmar check-out ───────────────────────────────────────────────
    pending_out = get_shift(conn, phone, "pending_gps_out")
    if pending_out:
        checkout_req = _tz(pending_out.get("check_out_at"))
        if checkout_req:
            elapsed = (now - checkout_req).total_seconds() / 60
            if elapsed > GPS_WINDOW_MIN:
                ex(conn, """
                    UPDATE public.event_timesheets
                    SET status='active', check_out_at=NULL, updated_at=NOW()
                    WHERE id=%s
                """, (pending_out["id"],))
                conn.commit()
                log.warning("GPS_EXPIRED check-out phone=%s elapsed=%.1fm", phone, elapsed)
                return (
                    f"⏱ Tempo expirado ({GPS_WINDOW_MIN} min).\n"
                    f"Envia *fim* novamente e partilha a localização de seguida."
                )

        start    = _tz(pending_out["start_time"])
        hours    = max(0.25, (now - start).total_seconds() / 3600)
        car_used = bool(pending_out.get("car_used"))
        rate     = _get_rate(conn)
        calc     = _calc(hours, car_used, rate)
        token    = str(uuid.uuid4())

        worker_name = pending_out.get("worker_id", "Colaborador")
        log_date    = pending_out.get("log_date") or dt.date.today()
        date_str    = log_date.strftime("%d/%m/%Y") if hasattr(log_date, "strftime") else str(log_date)[:10]
        start_str   = start.strftime("%H:%M")
        end_str     = now.strftime("%H:%M")
        location_in = pending_out.get("location") or location_str
        val_url     = f"{PUBLIC_BASE}/validar/{token}"

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE public.event_timesheets
                SET check_out_at     = %s,
                    hours            = %s,
                    days_equivalent  = %s,
                    worker_pay       = %s,
                    invoice_net      = %s,
                    invoice_vat      = %s,
                    invoice_total    = %s,
                    validation_token = %s,
                    status           = 'submitted',
                    gps_lat          = %s,
                    gps_lon          = %s,
                    gps_source       = 'whatsapp_gps',
                    updated_at       = NOW()
                WHERE id = %s
            """, (now, round(hours, 2), calc["days"],
                  calc["worker_pay"], calc["invoice_net"], calc["invoice_vat"],
                  calc["invoice_total"], token,
                  lat_f, lon_f, pending_out["id"]))
        conn.commit()

        # Notificar cliente (WhatsApp + Email)
        service_note = (pending_out.get("notes") or "").strip()
        worker_obj   = lookup_worker(conn, phone)
        client_phone = (worker_obj.get("default_client_phone") or DEFAULT_CLIENT or "").strip()
        client_email, email_source = _resolve_email_recipient(
            conn, pending_out["id"], phone)
        note_line_txt  = _fmt_notes_wa(service_note)
        note_block_email = _fmt_notes_html(service_note)

        client_name_out = (worker_obj.get("client_name") or "").strip()
        cli_greeting_out = f"Olá {client_name_out}!\n" if client_name_out else ""
        if client_phone:
            client_msg = (
                f"{cli_greeting_out}"
                f"📋 *Serviço registado*\n"
                f"👤 {worker_name}\n"
                f"📅 {date_str}\n"
                f"⏱ {start_str} → {end_str}  ({hours:.1f}h)\n"
                f"📍 {location_in}\n"
                f"{note_line_txt}"
                f"💶 Valor a pagar: *{calc['invoice_total']:.2f}€* (IVA incl.)\n\n"
                f"Para confirmar o serviço:\n{val_url}"
            )
            service_ref = f"{worker_name} · {date_str} · {start_str}→{end_str}"
            client_sid = send_client_message(
                conn, client_phone, client_msg,
                content_sid=WA_TMPL_CHECKOUT,
                variables={"1": client_name_out or "Cliente",
                           "2": service_ref,
                           "3": val_url},
                ts_id=pending_out["id"],
            )
            if client_sid:
                ex(conn, """
                    UPDATE public.event_timesheets
                    SET client_message_sid=%s, client_phone=%s WHERE id=%s
                """, (client_sid, client_phone, pending_out["id"]))
                conn.commit()
            log.info("NOTIF CLIENTE WA %s token=%s", client_phone, token)
        cc_out = (worker_obj.get("cc_phone") or "").strip()
        if cc_out and cc_out != client_phone:
            days_label_cc = f"{calc['days']:.0f}d" if calc["days"] > 1 else "1d"
            cc_notes = _fmt_notes_wa(service_note)
            cc_msg = (
                f"✅ *{worker_name} terminou*\n"
                f"📅 {date_str}  ⏱ {start_str} → {end_str} ({hours:.1f}h)\n"
                f"📍 {location_in}\n"
                f"{cc_notes}"
                f"💶 {days_label_cc} · {calc['invoice_total']:.2f}EUR (IVA incl.)"
            )
            send_client_message(conn, cc_out, cc_msg)
            log.info("NOTIF CC CHECKOUT %s worker=%s", cc_out, worker_name)

        if client_email and _send_email:
            client_name_email = (worker_obj.get("client_name") or "").strip()
            email_greeting = f"<p>Olá {client_name_email},</p>" if client_name_email else ""
            email_html = f"""
<html><body style="font-family:sans-serif;color:#1e293b;max-width:600px;margin:auto">
<p style="color:#64748b;font-size:.85em;margin-bottom:.25rem">Ao cuidado de Ana Pereira</p>
<h2 style="color:#0f172a">Serviço registado — validação pendente</h2>
{email_greeting}
<table style="width:100%;border-collapse:collapse;margin:1rem 0">
  <tr><td style="padding:.4rem 0;color:#64748b;width:140px"><b>Colaborador</b></td><td>{worker_name}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Data</b></td><td>{date_str}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Horário</b></td><td>{start_str} → {end_str} ({hours:.1f}h)</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Local</b></td><td>{location_in}</td></tr>
  <tr><td style="padding:.4rem 0;color:#64748b"><b>Valor (c/IVA)</b></td><td><b style="font-size:1.2em">{calc['invoice_total']:.2f}€</b></td></tr>
</table>
{note_block_email}
<p style="margin:2rem 0">
  <a href="{val_url}" style="background:#16a34a;color:#fff;padding:.75rem 1.5rem;border-radius:6px;text-decoration:none;font-weight:bold">
    Validar serviço
  </a>
</p>
<p style="color:#94a3b8;font-size:.85em">Grupo JDL · joaodiogo@grupojdl.pt</p>
</body></html>"""
            note_subj = f" — {service_note}" if service_note else ""
            res = _send_email(client_email,
                              f"Serviço {date_str} — {worker_name}{note_subj} — validação",
                              email_html)
            log.info("NOTIF CLIENTE EMAIL %s source=%s ok=%s",
                     client_email, email_source, res.get("ok"))

        log.info("CHECK-OUT CONFIRMED phone=%s %.1fh days=%.1f pay=%.2f total=%.2f car=%s",
                 phone, hours, calc["days"], calc["worker_pay"], calc["invoice_total"], car_used)

        # Resposta ao worker: payout + carro (SEM valor fatura)
        first_out  = _first(worker_obj.get("worker_name", ""))
        greeting_out = f"Olá {first_out}! " if first_out else ""
        days_label = f"{calc['days']:.0f} dias" if calc["days"] > 1 else "1 dia"
        car_bonus  = float(rate.get("car_bonus_day", 10)) * calc["days"]
        car_line   = f"🚗 Carro (+{car_bonus:.0f}€)\n" if car_used else ""
        return (
            f"{greeting_out}✅ Saída registada: *{end_str}*\n"
            f"⏱ {hours:.1f}h → {days_label}\n"
            f"{car_line}"
            f"💶 A receber: *{calc['worker_pay']:.2f}€*\n"
            f"📩 Enviado ao cliente para validação.\n"
            f"Bom descanso! 🙌"
        )

    # ── 3. Turno activo — GPS silencioso (atualiza localização) ─────────────
    active = get_shift(conn, phone, "active")
    if active:
        ex(conn, """
            UPDATE public.event_timesheets
            SET gps_lat=%s, gps_lon=%s, gps_source='whatsapp_gps', updated_at=NOW()
            WHERE id=%s
        """, (lat_f, lon_f, active["id"]))
        conn.commit()
        log.info("GPS_UPDATE phone=%s loc=%s", phone, location_str)
        return ""   # sem resposta ao worker

    log.info("GPS_NO_PENDING phone=%s", phone)
    return "📍 Localização recebida, mas não há registo pendente.\nEnvia *inicio* para começar."

# ── Estado ────────────────────────────────────────────────────────────────────

def handle_estado(conn, phone: str) -> str:
    shift = get_open_shift(conn, phone)
    if shift:
        s   = shift["status"]
        sin = _tz(shift["start_time"])
        elapsed = ((_now_utc()) - sin).total_seconds() / 3600 if sin else 0
        sstr    = sin.strftime("%H:%M") if sin else "?"
        car     = " 🚗" if shift.get("car_used") else ""
        if s == "pending_gps_in":
            return f"⏳ Entrada pendente desde *{sstr}* — partilha a localização para confirmar."
        if s == "pending_gps_out":
            return f"⏳ Saída pendente — partilha a localização para fechar o turno."
        return (f"⏱ Turno aberto desde *{sstr}* ({elapsed:.1f}h){car}\n"
                f"📍 {shift.get('location') or 'GPS confirmado'}\n"
                f"Manda *fim* para fechar.")

    v    = _variants(phone)
    last = q1(conn, """
        SELECT worker_id, location, log_date, start_time, status, hours, invoice_total
        FROM public.event_timesheets
        WHERE worker_phone IN (%s,%s,%s) AND status IN ('submitted','approved','rejected')
        ORDER BY start_time DESC LIMIT 1
    """, v)
    if not last:
        return "Sem registos. Manda *inicio* para começar."
    ld  = last.get("log_date")
    ds  = ld.strftime("%d/%m/%Y") if hasattr(ld, "strftime") else str(ld)[:10]
    st  = {"submitted": "⏳ Pendente validação", "approved": "✅ Aprovado",
           "rejected": "❌ Rejeitado"}.get(last.get("status", ""), last.get("status", ""))
    return f"Último: {ds} · {last.get('location','?')} · {last.get('hours','?')}h · {st}"

# ── Registo ───────────────────────────────────────────────────────────────────

def handle_registo(conn, phone: str, name: str) -> str:
    if not name:
        return "Usa: *registo <teu nome>*\nEx: *registo João Silva*"
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM public.persons WHERE name ILIKE %s LIMIT 1",
                    (f"%{name}%",))
        person = cur.fetchone()
    people_id = person["id"] if person else None
    pname     = person["name"] if person else name
    ex(conn, """
        INSERT INTO public.worker_contacts (worker_name, whatsapp_phone, people_id)
        VALUES (%s, %s, %s)
        ON CONFLICT (whatsapp_phone) DO UPDATE
          SET worker_name=EXCLUDED.worker_name, people_id=EXCLUDED.people_id, active=TRUE
    """, (pname, phone, people_id))
    conn.commit()
    log.info("REGISTO phone=%s name=%s people_id=%s", phone, pname, people_id)
    return (
        f"✅ Registado como *{pname}*.\n\n"
        f"Como funciona:\n"
        f"1️⃣ Envia *inicio* → partilha localização → entrada ✅\n"
        f"2️⃣ Envia *fim* → partilha localização → saída ✅"
    )

# ── Nota ──────────────────────────────────────────────────────────────────────

def handle_nota(conn, phone: str, note: str) -> str:
    """Acumular nota no turno em curso (append, não sobrescreve)."""
    if not note:
        return "Usa: *nota <descrição>*\nEx: *nota Estoril Open*"
    shift = get_open_shift(conn, phone)
    if not shift:
        return "Sem turno aberto. Manda *inicio* para começar."
    ex(conn, """UPDATE public.event_timesheets
               SET notes = CASE WHEN notes IS NULL OR notes = '' THEN %s
                                ELSE notes || E'\\n' || %s END,
                   updated_at = NOW()
               WHERE id = %s""",
       (note, note, shift["id"]))
    conn.commit()
    log.info("NOTA phone=%s shift_id=%s note=%r", phone, shift["id"], note)
    return f"📝 Nota guardada: *{note}*"


def _fmt_notes_wa(notes: str) -> str:
    """Formatar notas acumuladas para WhatsApp (lista com bullets)."""
    if not notes:
        return ""
    lines = [l.strip() for l in notes.splitlines() if l.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return f"📝 {lines[0]}\n"
    return "📝 *Notas do dia:*\n" + "\n".join(f"  • {l}" for l in lines) + "\n"


def _fmt_notes_html(notes: str) -> str:
    """Formatar notas acumuladas para HTML (bloco verde com lista)."""
    if not notes:
        return ""
    lines = [l.strip() for l in notes.splitlines() if l.strip()]
    if not lines:
        return ""
    items = "".join(f"<li style='margin:.2rem 0'>{l}</li>" for l in lines)
    return (
        f'<div style="background:#f0fdf4;border-left:4px solid #16a34a;'
        f'padding:.75rem 1rem;margin:1rem 0;border-radius:4px">'
        f'<b style="color:#15803d">Notas do colaborador:</b>'
        f'<ul style="margin:.5rem 0;padding-left:1.5rem">{items}</ul></div>'
    )

# ── Comandos ──────────────────────────────────────────────────────────────────

CMD_INICIO  = {"inicio","entrada","entrar","start","comecar","abrir","cheguei","comeco","chegou","chegou"}
CMD_FIM     = {"fim","saida","sair","end","stop","terminar","terminei","acabei","fui","sai","fechar"}
CMD_ESTADO  = {"estado","status","ver","horas","quanto"}
CMD_REGISTO = {"registo","registar","register","cadastrar","cadastro"}
CMD_NOTA    = {"nota","note","servico","evento","descricao","desc"}
CMD_AJUDA   = {"ajuda","help","?","comandos","menu"}
CAR_WORDS   = {"carro","car","veiculo","viatura","automovel"}
GREETINGS   = {"ola","oi","hi","hello","bom dia","boa tarde","boa noite","hey","boas"}

HELP_TEXT = (
    "🤖 *Ponto WhatsApp — como funciona:*\n\n"
    "1️⃣ Envia *inicio* (ou: cheguei, entrar, start)\n"
    "   Ex: *inicio Estoril Open* · *inicio carro*\n"
    "2️⃣ Partilha a tua *localização* no WhatsApp\n"
    "   → Entrada confirmada ✅\n\n"
    "3️⃣ Envia *fim* (ou: sair, terminei, acabei)\n"
    "4️⃣ Partilha a tua *localização* no WhatsApp\n"
    "   → Saída confirmada ✅\n\n"
    "• *nota <texto>* — adicionar nota ao turno\n"
    "   Ex: *nota Evento Deloitte*\n"
    "• *estado* — ver turno actual\n"
    "• *registo <nome>* — ligar número à conta\n"
    "• *ajuda* — esta mensagem\n\n"
    "_Texto sozinho não basta — o GPS é obrigatório._"
)


def _norm(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower())
                   if unicodedata.category(c) != "Mn")


def _log_inbound(conn, phone: str, profile_name: str, body: str, watched: bool):
    """Persiste inbound no log + emite Telegram se número vigiado.

    Matching (por ordem de prioridade):
      1. client_contacts (por phone) → contact_type='client_contact'
      2. worker_contacts (por whatsapp_phone) → contact_type='worker'
    """
    # 1. Procurar em client_contacts
    cc = q1(conn, """
        SELECT cc.id, cc.client_id, cc.name
        FROM public.client_contacts cc
        WHERE cc.phone = %s
        LIMIT 1
    """, (phone,))

    if cc:
        contact_type = "client_contact"
        contact_id   = cc["id"]
        client_id    = cc["client_id"]
        contact_name = cc["name"]
    else:
        # 2. Procurar em worker_contacts
        wc = q1(conn, """
            SELECT wc.id, wc.people_id, wc.worker_name
            FROM public.worker_contacts wc
            WHERE wc.whatsapp_phone = %s
            LIMIT 1
        """, (phone,))
        contact_type = "worker"       if wc else None
        contact_id   = wc["id"]       if wc else None
        client_id    = wc["people_id"] if wc else None
        contact_name = wc["worker_name"] if wc else None

    ex(conn, """
        INSERT INTO public.whatsapp_inbound_log
               (from_phone, profile_name, body, client_id, watched, contact_type, contact_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (phone, profile_name or None, body or None, client_id, watched, contact_type, contact_id))
    conn.commit()

    now_str = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info("whatsapp_inbound %s", json.dumps({
        "event":         "whatsapp_inbound",
        "from":          phone,
        "profile_name":  profile_name,
        "body":          body,
        "received_at":   now_str,
        "contact_type":  contact_type,
        "contact_id":    contact_id,
        "contact_name":  contact_name,
        "client_id":     client_id,
        "matched":       bool(cc or contact_type),
        "watched":       watched,
    }, ensure_ascii=False))

    if watched:
        log.info("watched_whatsapp_inbound %s", json.dumps({
            "event": "watched_whatsapp_inbound",
            "from":  phone,
        }))
        display_name = contact_name or profile_name or phone
        msg_text = body.strip() if body and body.strip() else "[sem texto]"
        tg_text = (
            f"📩 <b>Cliente respondeu no WhatsApp</b>\n"
            f"Número: <code>{phone}</code>\n"
            f"Nome: {display_name}\n"
            f"Mensagem: <i>{msg_text}</i>\n"
            f"Hora: {now_str}"
        )
        _send_telegram(tg_text)


def process(phone: str, body: str, lat=None, lon=None, addr=None,
            raw_sid: str = "", profile_name: str = "") -> dict:
    log.info("INBOUND from=%s sid=%s body=%r lat=%s lon=%s mode=%s",
             phone, raw_sid, body, lat, lon, WHATSAPP_MODE)
    conn = db()
    try:
        body_stripped = body.strip()
        tokens_raw    = body_stripped.split()
        tokens_norm   = [_norm(t) for t in tokens_raw]
        cmd           = tokens_norm[0] if tokens_norm else ""

        watched = phone in WATCHED_NUMBERS

        # ── GPS-only (sem texto) ─────────────────────────────────────────────
        if lat and not body_stripped:
            if cmd not in CMD_REGISTO:
                if not lookup_worker(conn, phone):
                    log.warning("UNAUTHORIZED GPS from=%s", phone)
                    return {"ok": False, "reply": "Número não autorizado."}
            update_inbound_window(conn, phone)
            _log_inbound(conn, phone, profile_name, body, watched)
            reply = handle_gps_confirm(conn, phone, lat, lon, addr or "")
            return {"ok": True, "reply": reply}

        # ── Segurança: números autorizados ───────────────────────────────────
        if cmd not in CMD_REGISTO:
            if not lookup_worker(conn, phone):
                log.warning("UNAUTHORIZED from=%s body=%r", phone, body)
                # Ainda registar inbound (pode ser cliente a responder — janela 24h + alerta)
                update_inbound_window(conn, phone)
                _log_inbound(conn, phone, profile_name, body, watched)
                return {"ok": False, "reply": ""}

        # Registar janela de inbound (para decisão 24h em envios futuros)
        update_inbound_window(conn, phone)
        _log_inbound(conn, phone, profile_name, body, watched)

        # ── GPS + texto: confirmar GPS e continuar com o comando ─────────────
        if lat and body_stripped:
            handle_gps_confirm(conn, phone, lat, lon, addr or "")

        # ── Comandos ─────────────────────────────────────────────────────────
        if cmd in CMD_INICIO:
            rest     = tokens_raw[1:]   # palavras depois de "inicio"
            car_used = bool(rest) and _norm(rest[-1]) in CAR_WORDS
            if car_used:
                rest = rest[:-1]        # remover "carro" da nota
            note     = " ".join(rest).strip()
            worker   = lookup_worker(conn, phone)
            reply    = handle_inicio_request(conn, phone, worker, car_used, note)

        elif cmd in CMD_FIM:
            reply = handle_fim_request(conn, phone)

        elif cmd in CMD_ESTADO:
            reply = handle_estado(conn, phone)

        elif cmd in CMD_REGISTO:
            name  = " ".join(tokens_raw[1:]).strip()
            reply = handle_registo(conn, phone, name)

        elif cmd in CMD_NOTA:
            note  = " ".join(tokens_raw[1:]).strip()
            reply = handle_nota(conn, phone, note)

        elif cmd in CMD_AJUDA or cmd in {_norm(g) for g in GREETINGS}:
            reply = HELP_TEXT

        else:
            reply = "Não percebi 🤔\nManda *ajuda* para ver como funciona."

        # Worker ACK: via TwiML in-session (contorna WABA lock 63051)
        # send_whatsapp_message só para notificações a terceiros (cliente/CC)
        return {"ok": True, "reply": reply}

    except Exception as e:
        log.exception("Erro ao processar mensagem de %s", phone)
        send_whatsapp_message(phone, "Erro interno. Tenta de novo.")
        return {"ok": False, "reply": ""}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from",         dest="from_phone", required=True)
    parser.add_argument("--body",         default="")
    parser.add_argument("--lat",          default=None)
    parser.add_argument("--lon",          default=None)
    parser.add_argument("--addr",         default="")
    parser.add_argument("--sid",          default="")
    parser.add_argument("--profile-name", dest="profile_name", default="")
    args = parser.parse_args()
    result = process(args.from_phone, args.body, args.lat, args.lon, args.addr,
                     args.sid, args.profile_name)
    print(json.dumps(result, ensure_ascii=False))
