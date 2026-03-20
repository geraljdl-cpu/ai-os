#!/usr/bin/env python3
# Remover bin/ do sys.path para evitar shadowing do stdlib (ex: bin/secrets.py)
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

"""
commercial_engine.py — Módulo Comercial: pedidos, orçamentos, PDFs, email

Uso:
  python3 bin/commercial_engine.py parse "Texto do pedido"
  python3 bin/commercial_engine.py submit "Texto do pedido" [--source email|whatsapp|form]
  python3 bin/commercial_engine.py quote <request_id>
  python3 bin/commercial_engine.py get_request <request_id>
  python3 bin/commercial_engine.py get_quote <quote_id>
  python3 bin/commercial_engine.py approve <quote_id> [--by "nome"]
  python3 bin/commercial_engine.py send <quote_id>
  python3 bin/commercial_engine.py list [--status new|quoted|sent|won|lost]
  python3 bin/commercial_engine.py pdf <quote_id>
"""

import json
import os
import re
import smtplib
from datetime import datetime, timezone, date
from decimal import Decimal
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# ── Load env from /etc/aios.env ───────────────────────────────────────────────
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
AIOS_ROOT    = Path(os.environ.get("AIOS_ROOT", Path.home() / "ai-os"))
QUOTES_DIR   = AIOS_ROOT / "runtime" / "quotes"
QUOTES_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)

SMTP_HOST          = os.environ.get("SMTP_HOST", "smtp.zoho.eu")
SMTP_PORT          = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER          = os.environ.get("SMTP_USER", "")
SMTP_PASS          = os.environ.get("SMTP_PASS", "")
SMTP_COMMERCIAL    = os.environ.get("SMTP_COMMERCIAL", "comercial@grupojdl.pt")

# ── email_send import (after stdlib path fixup) ───────────────────────────────
_sys.path.insert(0, _bin_dir)
from email_send import send_email as _send_email  # noqa: E402

# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _row(r) -> dict:
    import datetime as _dt
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, _dt.datetime):
            d[k] = v.isoformat()
        elif isinstance(v, _dt.date):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(v)
    return d


# ── PDF helper ────────────────────────────────────────────────────────────────

def _s(text) -> str:
    """Sanitize text for fpdf latin-1 output."""
    return (str(text or "")
            .replace("\u20ac", "EUR")
            .replace("\u2014", "--")
            .replace("\u2013", "-")
            .replace("\u2192", "->")
            .replace("\u2714", "OK")
            .replace("\u25cb", "-")
            .replace("\u2705", "OK")
            .replace("\u274c", "X")
            .replace("\u00e7", "c")
            .encode("latin-1", errors="replace")
            .decode("latin-1"))


def _nowstr():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── PHASE 1: parse_request ────────────────────────────────────────────────────

_MONTH_MAP = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3,
    "abril": 4, "maio": 5, "junho": 6, "julho": 7,
    "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

_CITIES = [
    "Lisboa", "Porto", "Setubal", "Setúbal", "Braga", "Coimbra",
    "Faro", "Aveiro", "Viseu", "Leiria", "Évora", "Evora",
    "Funchal", "Ponta Delgada", "Almada", "Amadora", "Loures",
    "Cascais", "Sintra", "Oeiras", "Barreiro", "Seixal",
    "Moita", "Montijo", "Palmela", "Alcochete", "Sesimbra",
]

_COMPANY_SUFFIXES = r"\b(Lda|lda|SA|S\.A\.|Unipessoal|SGPS|Lda\.|S\.L\.)\b"

_EVENT_KEYWORDS = {
    "evento":       "evento",
    "conferencia":  "conferência",
    "conferência":  "conferência",
    "formacao":     "formação",
    "formação":     "formação",
    "instalacao":   "instalação",
    "instalação":   "instalação",
    "manutencao":   "manutenção",
    "manutenção":   "manutenção",
    "servico":      "serviço",
    "serviço":      "serviço",
    "auditoria":    "auditoria",
    "consultoria":  "consultoria",
    "workshop":     "workshop",
    "seminario":    "seminário",
    "seminário":    "seminário",
}


def parse_request(raw_text: str) -> dict:
    """
    Simple keyword/regex extraction from raw text. No AI calls.
    Returns dict with parsed fields + status.
    """
    text = raw_text.strip()
    result = {
        "customer_name":  None,
        "company_name":   None,
        "customer_email": None,
        "customer_phone": None,
        "event_type":     None,
        "location":       None,
        "event_date":     None,
        "start_time":     None,
        "end_time":       None,
    }

    # ── email ──────────────────────────────────────────────────────────────────
    m = re.search(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    if m:
        result["customer_email"] = m.group(0)

    # ── Portuguese phone: +351XXXXXXXXX or 9XXXXXXXX or 2XXXXXXXX ─────────────
    m = re.search(r"(?:\+351[\s\-]?)?(?:9[1236]\d{7}|2\d{8})", text)
    if m:
        result["customer_phone"] = re.sub(r"[\s\-]", "", m.group(0))

    # ── customer_name: explicit "Nome: X" or "nome X" ─────────────────────────
    m = re.search(r"[Nn]ome\s*[:]\s*([A-ZÀ-Ú][a-zA-ZÀ-ÿ]+(?:\s+[A-ZÀ-Ú][a-zA-ZÀ-ÿ]+)*)", text)
    if m:
        result["customer_name"] = m.group(1).strip()
    else:
        # Try to find a proper noun sequence (two+ capitalized words) not followed by company suffix
        for m in re.finditer(r"\b([A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][a-záéíóúâêîôûãõç]+(?:\s+[A-ZÁÉÍÓÚÂÊÎÔÛÃÕÇ][a-záéíóúâêîôûãõç]+)+)\b", text):
            candidate = m.group(1)
            # Skip if it looks like a city or company
            if any(c.lower() == candidate.lower() for c in _CITIES):
                continue
            if re.search(_COMPANY_SUFFIXES, candidate):
                continue
            result["customer_name"] = candidate
            break

    # ── company_name: explicit "Empresa: X" or matches company suffix ──────────
    m = re.search(r"[Ee]mpresa\s*[:]\s*(.+?)(?:\s*,|\s*$|\s*\n)", text)
    if m:
        result["company_name"] = m.group(1).strip()
    else:
        m = re.search(
            r"([A-ZÀ-Ú][A-Za-záéíóúâêîôûãõç\s&]+?" + _COMPANY_SUFFIXES + r"\.?)",
            text
        )
        if m:
            result["company_name"] = m.group(0).strip().rstrip(",")

    # ── event_type ─────────────────────────────────────────────────────────────
    text_lower = text.lower()
    for kw, label in _EVENT_KEYWORDS.items():
        if kw in text_lower:
            result["event_type"] = label
            break

    # ── location: explicit "Local: X" or known city ────────────────────────────
    m = re.search(r"[Ll]ocal\s*[:]\s*([^\n,]+)", text)
    if m:
        result["location"] = m.group(1).strip()
    else:
        for city in _CITIES:
            if re.search(r"\b" + re.escape(city) + r"\b", text, re.IGNORECASE):
                result["location"] = city
                break

    # ── event_date ─────────────────────────────────────────────────────────────
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.search(r"\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b", text)
    if m:
        try:
            result["event_date"] = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
        except ValueError:
            pass

    if not result["event_date"]:
        # YYYY-MM-DD
        m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
        if m:
            try:
                result["event_date"] = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                pass

    if not result["event_date"]:
        # "dia X de Mês" or "X de Mês"
        m = re.search(
            r"(?:dia\s+)?(\d{1,2})\s+de\s+(" + "|".join(_MONTH_MAP.keys()) + r")\b",
            text_lower
        )
        if m:
            day = int(m.group(1))
            month = _MONTH_MAP[m.group(2)]
            year = datetime.now(timezone.utc).year
            # If the month has already passed this year, assume next year
            now = datetime.now(timezone.utc)
            if month < now.month or (month == now.month and day < now.day):
                year += 1
            try:
                result["event_date"] = date(year, month, day).isoformat()
            except ValueError:
                pass

    # ── times: "das X às Y" or HH:MM patterns ────────────────────────────────
    m = re.search(r"das\s+(\d{1,2}(?:[:h]\d{2})?)\s+[àa]s?\s+(\d{1,2}(?:[:h]\d{2})?)", text_lower)
    if m:
        result["start_time"] = m.group(1).replace("h", ":").zfill(4)
        result["end_time"]   = m.group(2).replace("h", ":").zfill(4)
    else:
        m = re.search(r"\b(\d{1,2}:\d{2})\s*[–\-]\s*(\d{1,2}:\d{2})\b", text)
        if m:
            result["start_time"] = m.group(1)
            result["end_time"]   = m.group(2)
        else:
            # Single "às Xh" or "às X:YY" — only valid hours 0-23
            m = re.search(r"[àa]s?\s+((?:[01]?\d|2[0-3])(?:h\d{0,2}|:\d{2})?)\b", text_lower)
            if m:
                result["start_time"] = m.group(1).replace("h", ":").rstrip(":")

    # ── Determine status based on missing fields ───────────────────────────────
    missing = sum(1 for v in result.values() if v is None)
    status = "new_needs_review" if missing > 3 else "new"

    return {**result, "status": status}


# ── submit_request ─────────────────────────────────────────────────────────────

def submit_request(conn, raw_text: str, source: str = "manual", **kwargs) -> dict:
    """
    Parse raw_text, override with any explicit kwargs, and insert into DB.
    Returns {ok, request_id, parsed, status}.
    """
    from sqlalchemy import text as sa_text
    parsed = parse_request(raw_text)
    # Override parsed fields with explicit kwargs
    for key in ("customer_name", "company_name", "customer_email",
                "customer_phone", "event_type", "location",
                "event_date", "start_time", "end_time", "status"):
        if key in kwargs and kwargs[key] is not None:
            parsed[key] = kwargs[key]

    sql = sa_text("""
        INSERT INTO public.commercial_requests
            (source, customer_name, company_name, customer_email, customer_phone,
             event_type, location, event_date, start_time, end_time,
             raw_request, parsed_json, status)
        VALUES
            (:source, :customer_name, :company_name, :customer_email, :customer_phone,
             :event_type, :location, :event_date, :start_time, :end_time,
             :raw_request, cast(:parsed_json as jsonb), :status)
        RETURNING id
    """)
    with conn.begin() as _c:
        row = _c.execute(sql, {
            "source":         source,
            "customer_name":  parsed.get("customer_name"),
            "company_name":   parsed.get("company_name"),
            "customer_email": parsed.get("customer_email"),
            "customer_phone": parsed.get("customer_phone"),
            "event_type":     parsed.get("event_type"),
            "location":       parsed.get("location"),
            "event_date":     parsed.get("event_date"),
            "start_time":     parsed.get("start_time"),
            "end_time":       parsed.get("end_time"),
            "raw_request":    raw_text,
            "parsed_json":    json.dumps(parsed, ensure_ascii=False),
            "status":         parsed.get("status", "new"),
        }).first()
    return {
        "ok":        True,
        "request_id": row[0],
        "parsed":    parsed,
        "status":    parsed.get("status", "new"),
    }


# ── generate_quote ─────────────────────────────────────────────────────────────

def generate_quote(conn, request_id: int) -> dict:
    """
    Generate a quote for a request based on event_type and active price rules.
    Returns {ok, quote_id, quote_number, total, line_items}.
    """
    from sqlalchemy import text as sa_text

    with conn.connect() as c:
        req = c.execute(sa_text(
            "SELECT * FROM public.commercial_requests WHERE id = :id"
        ), {"id": request_id}).mappings().first()
        if not req:
            return {"ok": False, "error": f"pedido #{request_id} não encontrado"}

        price_rules = c.execute(sa_text(
            "SELECT * FROM public.commercial_price_rules WHERE active = TRUE ORDER BY category, code"
        )).mappings().all()

    rules_by_code = {r["code"]: dict(r) for r in price_rules}
    event_type = (req["event_type"] or "").lower()
    location   = (req["location"] or "").lower()

    line_items = []

    # ── Determine which rules to apply based on event_type ────────────────────
    service_keywords = ("servi", "manuten", "instala", "manutenção", "instalação",
                        "serviço", "manutencao", "instalacao")
    event_keywords   = ("evento", "conferên", "formação", "workshop", "seminário",
                        "conferencia", "formacao", "seminario")
    coord_keywords   = ("conferên", "workshop", "formação", "formacao", "seminário",
                        "seminario", "evento", "conferencia")

    is_service = any(kw in event_type for kw in service_keywords)
    is_event   = any(kw in event_type for kw in event_keywords)
    needs_coord = any(kw in event_type for kw in coord_keywords)

    if is_service or not event_type:
        # Default: day of labour
        rule = rules_by_code.get("MAN_OBR_DIA")
        if rule:
            qty = 1
            line_items.append({
                "code":               rule["code"],
                "description":        rule["description"],
                "qty":                qty,
                "unit":               rule["unit"],
                "unit_price":         float(rule["unit_price"]),
                "vat_rate":           float(rule["vat_rate"]),
                "line_total":         round(qty * float(rule["unit_price"]), 2),
                "needs_manual_price": False,
            })

    if is_event:
        # Add equipment
        rule = rules_by_code.get("EQUIP_BASIC")
        if rule:
            qty = 1
            line_items.append({
                "code":               rule["code"],
                "description":        rule["description"],
                "qty":                qty,
                "unit":               rule["unit"],
                "unit_price":         float(rule["unit_price"]),
                "vat_rate":           float(rule["vat_rate"]),
                "line_total":         round(qty * float(rule["unit_price"]), 2),
                "needs_manual_price": False,
            })

    if needs_coord:
        rule = rules_by_code.get("COORD_HR")
        if rule:
            qty = 4  # default 4 hours coordination
            line_items.append({
                "code":               rule["code"],
                "description":        rule["description"] + " (4h estimadas)",
                "qty":                qty,
                "unit":               rule["unit"],
                "unit_price":         float(rule["unit_price"]),
                "vat_rate":           float(rule["vat_rate"]),
                "line_total":         round(qty * float(rule["unit_price"]), 2),
                "needs_manual_price": False,
            })

    # ── Transport: add if location is far or not local ─────────────────────────
    far_cities = ("porto", "braga", "coimbra", "aveiro", "faro", "viseu",
                  "funchal", "ponta delgada")
    is_far = any(city in location for city in far_cities)
    if is_far or not location:
        rule = rules_by_code.get("TRANSP_FLAT")
        if rule:
            line_items.append({
                "code":               rule["code"],
                "description":        rule["description"],
                "qty":                1,
                "unit":               rule["unit"],
                "unit_price":         float(rule["unit_price"]),
                "vat_rate":           float(rule["vat_rate"]),
                "line_total":         float(rule["unit_price"]),
                "needs_manual_price": False,
            })
    elif location:
        rule = rules_by_code.get("TRANSP_KM")
        if rule:
            line_items.append({
                "code":               rule["code"],
                "description":        rule["description"] + " (distância a confirmar)",
                "qty":                0,
                "unit":               rule["unit"],
                "unit_price":         float(rule["unit_price"]),
                "vat_rate":           float(rule["vat_rate"]),
                "line_total":         0.0,
                "needs_manual_price": True,
            })

    # ── Fallback: if no items generated, add generic placeholder ─────────────
    if not line_items:
        line_items.append({
            "code":               "GENERIC",
            "description":        "Serviço (a definir com cliente)",
            "qty":                1,
            "unit":               "un",
            "unit_price":         0.0,
            "vat_rate":           23.0,
            "line_total":         0.0,
            "needs_manual_price": True,
        })

    # ── Calculate totals ──────────────────────────────────────────────────────
    subtotal   = round(sum(item["line_total"] for item in line_items), 2)
    # Weighted VAT: sum(line_total * vat_rate) / subtotal if subtotal > 0
    if subtotal > 0:
        vat_amount = round(
            sum(item["line_total"] * item["vat_rate"] / 100 for item in line_items), 2
        )
    else:
        vat_amount = 0.0
    total = round(subtotal + vat_amount, 2)

    # ── Generate quote number: ORC-YYYY-NNNN ─────────────────────────────────
    year = datetime.now(timezone.utc).year
    with conn.connect() as c:
        seq_row = c.execute(sa_text(
            "SELECT COUNT(*) FROM public.commercial_quotes WHERE quote_number LIKE :prefix"
        ), {"prefix": f"ORC-{year}-%"}).first()
    seq = (seq_row[0] if seq_row else 0) + 1
    quote_number = f"ORC-{year}-{seq:04d}"

    title = f"Orçamento — {req.get('event_type') or 'Serviço'}"
    if req.get("company_name"):
        title += f" para {req['company_name']}"
    elif req.get("customer_name"):
        title += f" para {req['customer_name']}"

    ins = sa_text("""
        INSERT INTO public.commercial_quotes
            (request_id, quote_number, title, line_items, subtotal, vat_amount, total, status)
        VALUES
            (:request_id, :quote_number, :title, cast(:line_items as jsonb),
             :subtotal, :vat_amount, :total, 'draft')
        RETURNING id
    """)
    with conn.begin() as c:
        qrow = c.execute(ins, {
            "request_id":   request_id,
            "quote_number": quote_number,
            "title":        title,
            "line_items":   json.dumps(line_items, ensure_ascii=False),
            "subtotal":     subtotal,
            "vat_amount":   vat_amount,
            "total":        total,
        }).first()
        # Update request status to 'quoted'
        c.execute(sa_text(
            "UPDATE public.commercial_requests SET status='quoted', updated_at=NOW() WHERE id=:id"
        ), {"id": request_id})

    return {
        "ok":          True,
        "quote_id":    qrow[0],
        "quote_number": quote_number,
        "total":       total,
        "subtotal":    subtotal,
        "vat_amount":  vat_amount,
        "line_items":  line_items,
    }


# ── get_request ────────────────────────────────────────────────────────────────

def get_request(conn, request_id: int) -> dict:
    """Fetch request + associated quotes."""
    from sqlalchemy import text as sa_text
    with conn.connect() as c:
        req = c.execute(sa_text(
            "SELECT * FROM public.commercial_requests WHERE id = :id"
        ), {"id": request_id}).mappings().first()
        if not req:
            return {"ok": False, "error": f"pedido #{request_id} não encontrado"}
        quotes = c.execute(sa_text(
            "SELECT * FROM public.commercial_quotes WHERE request_id = :id ORDER BY id DESC"
        ), {"id": request_id}).mappings().all()
    result = _row(req)
    result["quotes"] = [_row(q) for q in quotes]
    result["ok"] = True
    return result


# ── list_requests ──────────────────────────────────────────────────────────────

def list_requests(conn, status: str = None, limit: int = 20) -> list:
    """List requests with optional status filter."""
    from sqlalchemy import text as sa_text
    where = "WHERE r.status = :status" if status else "WHERE TRUE"
    params = {"n": limit}
    if status:
        params["status"] = status
    with conn.connect() as c:
        rows = c.execute(sa_text(f"""
            SELECT r.id, r.created_at, r.source, r.customer_name, r.company_name,
                   r.customer_email, r.event_type, r.event_date, r.status,
                   r.assigned_to,
                   COUNT(q.id) AS quote_count,
                   MAX(q.total) AS quote_total
            FROM public.commercial_requests r
            LEFT JOIN public.commercial_quotes q ON q.request_id = r.id
            {where}
            GROUP BY r.id
            ORDER BY r.created_at DESC
            LIMIT :n
        """), params).mappings().all()
    return [_row(r) for r in rows]


# ── get_quote ──────────────────────────────────────────────────────────────────

def get_quote(conn, quote_id: int) -> dict:
    """Fetch quote with associated request data."""
    from sqlalchemy import text as sa_text
    with conn.connect() as c:
        q = c.execute(sa_text("""
            SELECT cq.*, cr.customer_name, cr.company_name, cr.customer_email,
                   cr.customer_phone, cr.event_type, cr.location, cr.event_date
            FROM public.commercial_quotes cq
            JOIN public.commercial_requests cr ON cr.id = cq.request_id
            WHERE cq.id = :id
        """), {"id": quote_id}).mappings().first()
    if not q:
        return {"ok": False, "error": f"orçamento #{quote_id} não encontrado"}
    result = _row(q)
    result["ok"] = True
    return result


# ── update_quote_items ─────────────────────────────────────────────────────────

def update_quote_items(conn, quote_id: int, line_items: list,
                       assumptions: str = None, exclusions: str = None) -> dict:
    """Update line items and recalculate totals. Status stays 'draft'."""
    from sqlalchemy import text as sa_text

    # Recalculate totals
    subtotal = round(sum(
        float(item.get("line_total", 0) or
              float(item.get("qty", 0)) * float(item.get("unit_price", 0)))
        for item in line_items
    ), 2)
    vat_amount = round(
        sum(
            float(item.get("line_total", 0) or
                  float(item.get("qty", 0)) * float(item.get("unit_price", 0)))
            * float(item.get("vat_rate", 23)) / 100
            for item in line_items
        ), 2
    )
    total = round(subtotal + vat_amount, 2)

    params = {
        "id":          quote_id,
        "line_items":  json.dumps(line_items, ensure_ascii=False),
        "subtotal":    subtotal,
        "vat_amount":  vat_amount,
        "total":       total,
    }
    set_clauses = "line_items=:line_items::jsonb, subtotal=:subtotal, vat_amount=:vat_amount, total=:total, updated_at=NOW()"
    if assumptions is not None:
        set_clauses += ", assumptions=:assumptions"
        params["assumptions"] = assumptions
    if exclusions is not None:
        set_clauses += ", exclusions=:exclusions"
        params["exclusions"] = exclusions

    with conn.begin() as c:
        c.execute(sa_text(
            f"UPDATE public.commercial_quotes SET {set_clauses} WHERE id=:id"
        ), params)

    return {"ok": True, "quote_id": quote_id, "subtotal": subtotal,
            "vat_amount": vat_amount, "total": total}


# ── approve_quote ──────────────────────────────────────────────────────────────

def approve_quote(conn, quote_id: int, approved_by: str = "system") -> dict:
    """Set status='approved', record who approved and when."""
    from sqlalchemy import text as sa_text
    with conn.begin() as c:
        result = c.execute(sa_text("""
            UPDATE public.commercial_quotes
            SET status='approved', approved_at=NOW(), approved_by=:by, updated_at=NOW()
            WHERE id=:id
            RETURNING id, quote_number, total
        """), {"id": quote_id, "by": approved_by}).first()
    if not result:
        return {"ok": False, "error": f"orçamento #{quote_id} não encontrado"}
    return {"ok": True, "quote_id": result[0], "quote_number": result[1],
            "total": float(result[2]), "approved_by": approved_by}


# ── generate_quote_pdf ─────────────────────────────────────────────────────────

def generate_quote_pdf(conn, quote_id: int) -> str:
    """
    Generate PDF for a quote using fpdf. Returns the file path.
    """
    from fpdf import FPDF

    q = get_quote(conn, quote_id)
    if not q.get("ok"):
        raise ValueError(q.get("error", "Orçamento não encontrado"))

    quote_number = q["quote_number"]
    now_str      = _nowstr()
    fname        = f"QUOTE-{quote_number}.pdf"
    fpath        = QUOTES_DIR / fname

    # Parse line_items (may be string from DB)
    line_items = q.get("line_items", [])
    if isinstance(line_items, str):
        line_items = json.loads(line_items)

    client_name  = q.get("company_name") or q.get("customer_name") or "—"
    client_email = q.get("customer_email") or "—"
    client_phone = q.get("customer_phone") or "—"
    event_type   = q.get("event_type") or "—"
    location     = q.get("location") or "—"
    event_date   = str(q.get("event_date") or "—")[:10]

    pdf = FPDF()
    pdf.add_page()
    pdf.set_margins(15, 15, 15)

    # ── Header ────────────────────────────────────────────────────────────────
    pdf.set_fill_color(11, 15, 30)
    pdf.rect(0, 0, 210, 35, "F")
    pdf.set_font("Helvetica", "B", 20)
    pdf.set_text_color(255, 255, 255)
    pdf.set_xy(15, 7)
    pdf.cell(80, 12, "GRUPO JDL")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_xy(110, 7)
    pdf.cell(0, 6, _s(f"Orcamento Nr: {quote_number}"), align="R")
    pdf.set_xy(110, 13)
    pdf.cell(0, 6, _s(f"Data: {now_str[:10]}"), align="R")
    pdf.set_xy(110, 19)
    pdf.set_text_color(160, 180, 220)
    pdf.cell(0, 6, "comercial@grupojdl.pt", align="R")
    pdf.set_text_color(30, 30, 30)
    pdf.set_xy(15, 40)

    # ── Title ─────────────────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(11, 15, 30)
    pdf.cell(0, 8, _s(q.get("title") or f"Orcamento {quote_number}"),
             new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ── Client info table ─────────────────────────────────────────────────────
    pdf.set_fill_color(240, 244, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 60, 130)
    pdf.cell(0, 7, "DADOS DO CLIENTE", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.set_text_color(30, 30, 30)

    def _info_row(label, value):
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(100, 110, 130)
        pdf.cell(50, 6, _s(label) + ":")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 6, _s(value), new_x="LMARGIN", new_y="NEXT")

    _info_row("Empresa / Cliente", client_name)
    _info_row("Email",             client_email)
    _info_row("Telefone",          client_phone)
    _info_row("Tipo de Servico",   event_type)
    _info_row("Local",             location)
    _info_row("Data do Servico",   event_date)
    pdf.ln(4)

    # ── Line items table ──────────────────────────────────────────────────────
    pdf.set_fill_color(240, 244, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(30, 60, 130)
    pdf.cell(0, 7, "DESCRICAO DOS SERVICOS", new_x="LMARGIN", new_y="NEXT", fill=True)
    pdf.ln(1)

    # Table header
    pdf.set_fill_color(220, 230, 255)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(30, 60, 130)
    col_w = [80, 15, 15, 25, 15, 30]
    headers = ["Descricao", "Qtd", "Unid", "Preco Unit", "IVA%", "Total"]
    for h, w in zip(headers, col_w):
        pdf.cell(w, 7, h, border=1, fill=True, align="C")
    pdf.ln()

    # Rows
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(20, 20, 20)
    row_fill = False
    for item in line_items:
        needs_manual = item.get("needs_manual_price", False)
        qty         = item.get("qty", 1)
        unit_price  = float(item.get("unit_price", 0))
        line_total  = float(item.get("line_total", 0))
        vat_rate    = float(item.get("vat_rate", 23))
        desc        = item.get("description", "—")

        if needs_manual:
            pdf.set_text_color(180, 120, 0)  # amber for needs_manual
            pdf.set_font("Helvetica", "I", 9)
        else:
            pdf.set_text_color(20, 20, 20)
            pdf.set_font("Helvetica", "", 9)

        fill_color = (250, 252, 255) if row_fill else (255, 255, 255)
        pdf.set_fill_color(*fill_color)

        pdf.cell(col_w[0], 6, _s(desc[:55]), border=1, fill=True)
        pdf.cell(col_w[1], 6, str(qty) if not needs_manual else "?",
                 border=1, fill=True, align="C")
        pdf.cell(col_w[2], 6, _s(item.get("unit", "un")),
                 border=1, fill=True, align="C")
        price_str = f"EUR {unit_price:.2f}" if not needs_manual else "A definir"
        pdf.cell(col_w[3], 6, _s(price_str), border=1, fill=True, align="R")
        pdf.cell(col_w[4], 6, f"{vat_rate:.0f}%", border=1, fill=True, align="C")
        total_str = f"EUR {line_total:.2f}" if not needs_manual else "A definir"
        pdf.cell(col_w[5], 6, _s(total_str), border=1, fill=True, align="R")
        pdf.ln()
        row_fill = not row_fill

    # Reset text color
    pdf.set_text_color(20, 20, 20)
    pdf.set_font("Helvetica", "", 9)
    pdf.ln(2)

    # ── Totals box ────────────────────────────────────────────────────────────
    box_x = 110
    pdf.set_x(box_x)
    pdf.set_fill_color(240, 244, 255)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(50, 7, "Subtotal:", fill=True, border=1, align="R")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(30, 7, _s(f"EUR {float(q.get('subtotal', 0)):.2f}"),
             fill=True, border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(box_x)
    pdf.set_fill_color(240, 244, 255)
    pdf.cell(50, 7, "IVA:", fill=True, border=1, align="R")
    pdf.cell(30, 7, _s(f"EUR {float(q.get('vat_amount', 0)):.2f}"),
             fill=True, border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.set_x(box_x)
    pdf.set_fill_color(11, 15, 30)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(50, 9, "TOTAL:", fill=True, border=1, align="R")
    pdf.cell(30, 9, _s(f"EUR {float(q.get('total', 0)):.2f}"),
             fill=True, border=1, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    pdf.ln(4)

    # ── Assumptions & Exclusions ──────────────────────────────────────────────
    assumptions = q.get("assumptions")
    exclusions  = q.get("exclusions")

    if assumptions:
        pdf.set_fill_color(240, 244, 255)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(30, 60, 130)
        pdf.cell(0, 7, "PRESSUPOSTOS", new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 5, _s(assumptions))
        pdf.ln(2)

    if exclusions:
        pdf.set_fill_color(255, 245, 240)
        pdf.set_font("Helvetica", "B", 10)
        pdf.set_text_color(160, 60, 30)
        pdf.cell(0, 7, "EXCLUSOES", new_x="LMARGIN", new_y="NEXT", fill=True)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(0, 5, _s(exclusions))
        pdf.ln(2)

    # ── Footer ────────────────────────────────────────────────────────────────
    validity = q.get("validity_days", 30)
    pdf.set_y(-30)
    pdf.set_fill_color(240, 244, 255)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 110, 130)
    pdf.cell(0, 5, _s(f"Este orcamento tem validade de {validity} dias a partir da data de emissao."),
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.cell(0, 4, "comercial@grupojdl.pt  |  GRUPO JDL",
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", "I", 7)
    pdf.cell(0, 4, _s(f"Gerado automaticamente pelo AI-OS em {now_str}"),
             new_x="LMARGIN", new_y="NEXT", align="C")

    pdf.output(str(fpath))

    # Update pdf_path in DB
    from sqlalchemy import text as sa_text
    with conn.begin() as c:
        c.execute(sa_text(
            "UPDATE public.commercial_quotes SET pdf_path=:p, updated_at=NOW() WHERE id=:id"
        ), {"p": str(fpath), "id": quote_id})

    return str(fpath)


# ── send_quote_email ───────────────────────────────────────────────────────────

def send_quote_email(conn, quote_id: int) -> dict:
    """
    Send quote by email. ONLY works if quote.status == 'approved'.
    """
    from sqlalchemy import text as sa_text

    q = get_quote(conn, quote_id)
    if not q.get("ok"):
        return {"ok": False, "error": q.get("error")}

    if q.get("status") != "approved":
        return {
            "ok": False,
            "error": "Quote must be approved before sending",
            "current_status": q.get("status"),
        }

    to_email = q.get("customer_email")
    if not to_email:
        return {"ok": False, "error": "Sem email do cliente no pedido"}

    # Generate PDF if not yet done
    pdf_path = q.get("pdf_path")
    if not pdf_path or not Path(pdf_path).exists():
        pdf_path = generate_quote_pdf(conn, quote_id)

    quote_number = q["quote_number"]
    company      = q.get("company_name") or q.get("customer_name") or "Cliente"
    total        = float(q.get("total", 0))
    validity     = q.get("validity_days", 30)
    event_type   = q.get("event_type") or "Serviço"
    event_date   = str(q.get("event_date") or "")[:10] or "A confirmar"
    location     = q.get("location") or "A confirmar"

    # Line items summary for email
    line_items = q.get("line_items", [])
    if isinstance(line_items, str):
        line_items = json.loads(line_items)

    rows_html = ""
    for item in line_items:
        needs_manual = item.get("needs_manual_price", False)
        desc = item.get("description", "—")
        qty  = item.get("qty", 1)
        unit = item.get("unit", "un")
        total_item = f"EUR {float(item.get('line_total', 0)):.2f}" if not needs_manual else "A definir"
        rows_html += f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;">{desc}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:center;">{qty} {unit}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #eee;text-align:right;
                     {'color:#b87800;font-style:italic' if needs_manual else ''}">{total_item}</td>
        </tr>"""

    subject = f"Orçamento {quote_number} — {company}"
    html_body = f"""
<!DOCTYPE html>
<html lang="pt">
<head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;color:#222;max-width:640px;margin:0 auto;padding:20px;">
  <div style="background:#0b0f1e;padding:20px 30px;border-radius:6px 6px 0 0;">
    <h1 style="color:#fff;margin:0;font-size:22px;">GRUPO JDL</h1>
    <p style="color:#a0b4dc;margin:4px 0 0;">comercial@grupojdl.pt</p>
  </div>
  <div style="background:#f4f6ff;padding:20px 30px;">
    <h2 style="color:#0b0f1e;margin:0 0 10px;">Orçamento {quote_number}</h2>
    <p style="color:#555;">Exmo(a) Sr(a), {company},</p>
    <p>Enviamos em anexo o orçamento relativo ao serviço solicitado. Seguem os detalhes:</p>
    <table style="width:100%;border-collapse:collapse;margin:15px 0;background:#fff;
                  border-radius:4px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08);">
      <tr style="background:#e8edff;">
        <th style="padding:8px 10px;text-align:left;font-size:12px;color:#1e3c82;">Tipo</th>
        <td style="padding:8px 10px;">{event_type}</td>
      </tr>
      <tr>
        <th style="padding:8px 10px;text-align:left;font-size:12px;color:#1e3c82;">Local</th>
        <td style="padding:8px 10px;">{location}</td>
      </tr>
      <tr style="background:#e8edff;">
        <th style="padding:8px 10px;text-align:left;font-size:12px;color:#1e3c82;">Data</th>
        <td style="padding:8px 10px;">{event_date}</td>
      </tr>
    </table>

    <h3 style="color:#1e3c82;margin:20px 0 8px;">Resumo dos Serviços</h3>
    <table style="width:100%;border-collapse:collapse;background:#fff;
                  border-radius:4px;box-shadow:0 1px 4px rgba(0,0,0,.08);">
      <thead>
        <tr style="background:#1e3c82;color:#fff;">
          <th style="padding:8px 10px;text-align:left;">Descrição</th>
          <th style="padding:8px 10px;text-align:center;">Qtd</th>
          <th style="padding:8px 10px;text-align:right;">Total</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>

    <div style="margin:20px 0;text-align:right;">
      <div style="display:inline-block;background:#0b0f1e;color:#fff;
                  padding:12px 24px;border-radius:4px;font-size:18px;font-weight:bold;">
        TOTAL: EUR {total:.2f}
      </div>
    </div>
    <p style="color:#888;font-size:12px;">
      Este orçamento tem validade de {validity} dias a partir da data de emissão.<br>
      O documento completo encontra-se em anexo neste email.
    </p>
    <p>Para qualquer questão, não hesite em contactar-nos:<br>
       <a href="mailto:comercial@grupojdl.pt">comercial@grupojdl.pt</a>
    </p>
    <p>Com os melhores cumprimentos,<br><strong>Equipa Comercial — GRUPO JDL</strong></p>
  </div>
  <div style="background:#e8edff;padding:10px 30px;border-radius:0 0 6px 6px;
              text-align:center;font-size:11px;color:#888;">
    GRUPO JDL — comercial@grupojdl.pt
  </div>
</body>
</html>"""

    # Send with attachment (direct SMTP — email_send.py doesn't support attachments)
    if not SMTP_USER or not SMTP_PASS:
        return {"ok": False, "error": "SMTP not configured (SMTP_USER/SMTP_PASS missing)"}

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"]    = SMTP_COMMERCIAL if SMTP_COMMERCIAL else SMTP_USER
        msg["To"]      = to_email

        # HTML part
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)

        # PDF attachment
        with open(pdf_path, "rb") as f:
            pdf_data = f.read()
        pdf_part = MIMEBase("application", "pdf")
        pdf_part.set_payload(pdf_data)
        encoders.encode_base64(pdf_part)
        pdf_part.add_header(
            "Content-Disposition",
            f'attachment; filename="{Path(pdf_path).name}"'
        )
        msg.attach(pdf_part)

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(SMTP_USER, SMTP_PASS)
            smtp.sendmail(SMTP_COMMERCIAL or SMTP_USER, [to_email], msg.as_string())

        # Update DB
        with conn.begin() as upd:
            upd.execute(sa_text("""
                UPDATE public.commercial_quotes
                SET email_sent_at=NOW(), status='sent', updated_at=NOW()
                WHERE id=:id
            """), {"id": quote_id})
            upd.execute(sa_text("""
                UPDATE public.commercial_requests
                SET status='sent', updated_at=NOW()
                WHERE id=(SELECT request_id FROM public.commercial_quotes WHERE id=:id)
            """), {"id": quote_id})

        return {
            "ok":           True,
            "quote_id":     quote_id,
            "quote_number": quote_number,
            "email_sent_to": to_email,
        }

    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── CLI ────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Commercial Engine CLI")
    parser.add_argument("command", choices=[
        "parse", "submit", "quote", "get_request", "get_quote",
        "approve", "send", "list", "pdf",
    ])
    parser.add_argument("arg", nargs="?", help="Main argument (text or ID)")
    parser.add_argument("--source", default="manual",
                        help="Source: manual|email|whatsapp|form")
    parser.add_argument("--by", default="system",
                        help="Approved by (for approve command)")
    parser.add_argument("--status", default=None,
                        help="Status filter (for list command)")

    args = parser.parse_args()
    cmd  = args.command

    if cmd == "parse":
        if not args.arg:
            print(json.dumps({"error": "Forneça o texto do pedido"}))
            _sys.exit(1)
        result = parse_request(args.arg)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "submit":
        if not args.arg:
            print(json.dumps({"error": "Forneça o texto do pedido"}))
            _sys.exit(1)
        engine, _ = _conn()
        result = submit_request(engine, args.arg, source=args.source)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "quote":
        if not args.arg:
            print(json.dumps({"error": "Forneça o request_id"}))
            _sys.exit(1)
        engine, _ = _conn()
        result = generate_quote(engine, int(args.arg))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "get_request":
        if not args.arg:
            print(json.dumps({"error": "Forneça o request_id"}))
            _sys.exit(1)
        engine, _ = _conn()
        result = get_request(engine, int(args.arg))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "get_quote":
        if not args.arg:
            print(json.dumps({"error": "Forneça o quote_id"}))
            _sys.exit(1)
        engine, _ = _conn()
        result = get_quote(engine, int(args.arg))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "approve":
        if not args.arg:
            print(json.dumps({"error": "Forneça o quote_id"}))
            _sys.exit(1)
        engine, _ = _conn()
        result = approve_quote(engine, int(args.arg), approved_by=args.by)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "send":
        if not args.arg:
            print(json.dumps({"error": "Forneça o quote_id"}))
            _sys.exit(1)
        engine, _ = _conn()
        result = send_quote_email(engine, int(args.arg))
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "list":
        engine, _ = _conn()
        result = list_requests(engine, status=args.status)
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == "pdf":
        if not args.arg:
            print(json.dumps({"error": "Forneça o quote_id"}))
            _sys.exit(1)
        engine, _ = _conn()
        path = generate_quote_pdf(engine, int(args.arg))
        print(json.dumps({"ok": True, "path": path}, ensure_ascii=False, indent=2))

    else:
        print(json.dumps({"error": f"Comando desconhecido: {cmd}"}))
        _sys.exit(1)
