#!/usr/bin/env python3
# Remover bin/ do sys.path para evitar shadowing do stdlib
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

"""
insurance_engine.py — Módulo Seguros: apólices, documentos, alertas

Uso:
  python3 bin/insurance_engine.py add --insurer NAME --policy-number X --entity-type vehicle --entity-ref AA-00-BB --start YYYY-MM-DD --end YYYY-MM-DD --premium 450.00 [--category automovel]
  python3 bin/insurance_engine.py list [--status active] [--type vehicle] [--limit 20]
  python3 bin/insurance_engine.py get <id>
  python3 bin/insurance_engine.py update <id> --end YYYY-MM-DD [--status active]
  python3 bin/insurance_engine.py add-doc <policy_id> --type policy [--file /path] [--amount 450]
  python3 bin/insurance_engine.py ingest <file.txt>
  python3 bin/insurance_engine.py generate-alerts
  python3 bin/insurance_engine.py stats
"""

import argparse
import datetime as dt
import json
import os
import re
from decimal import Decimal
from pathlib import Path

# ── Load env ──────────────────────────────────────────────────────────────────
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Config ────────────────────────────────────────────────────────────────────
AIOS_ROOT    = Path(os.environ.get("AIOS_ROOT", Path.home() / "ai-os"))
UPLOADS_DIR  = AIOS_ROOT / "runtime" / "insurance"
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)

# ── DB helpers ────────────────────────────────────────────────────────────────

def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _row(r) -> dict:
    d = dict(r._mapping) if hasattr(r, '_mapping') else dict(r)
    for k, v in d.items():
        if isinstance(v, dt.datetime):
            d[k] = v.isoformat()
        elif isinstance(v, dt.date):
            d[k] = v.isoformat()
        elif isinstance(v, Decimal):
            d[k] = float(v)
    return d


# ── Document parser ───────────────────────────────────────────────────────────

PATTERNS = {
    "policy_number": r'(?:ap[oó]lice\s*n[ºo°]?|policy)[^\d]{0,5}([\d\-/]{5,20})',
    "plate":         r'\b([A-Z]{2}-?\d{2}-?[A-Z]{2}|\d{2}-?\d{2}-?[A-Z]{2}|\d{2}-?[A-Z]{2}-?\d{2})\b',
    "nif":           r'\bNIF[:\s]*(\d{9})\b',
    "start_date":    r'(?:in[ií]cio|v[aá]lido\s*de)[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    "end_date":      r'(?:fim|validade|v[aá]lido\s*at[eé]|at[eé])[:\s]+(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})',
    "premium":       r'(?:pr[eé]mio|premio|valor\s*anual)[:\s]*(\d[\d.,]+)\s*[€E]',
    "insurer":       r'(Fidelidade|Tranquilidade|Zurich|Allianz|Ageas|Generali|AXA|Liberty|Ocidental|Lus[ií]t[aâ]nia|GNB\s*Seguros|Ok\s*Teleseguros|Multicare)',
}


def _parse_date(s: str):
    """Parse DD/MM/YYYY or DD-MM-YYYY or YYYY-MM-DD to date."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _parse_carta_verde(text: str) -> dict:
    """Parser específico para cartas verdes Fidelidade.
    Os dígitos aparecem separados por \\n\\n no texto extraído do PDF.
    Extrai: plate, insurer, policy_number, start_date, end_date, vehicle_model."""
    result = {}

    # Seguradora
    m = re.search(r'(Fidelidade|Tranquilidade|Zurich|Allianz|Ageas|Generali|AXA|Liberty|Ocidental)', text, re.I)
    if m:
        result["insurer"] = m.group(1)

    # Matrícula — formato PT: AA-00-AA, 00-00-AA, AA-00-00
    plates = re.findall(r'\b([A-Z]{2}-\d{2}-[A-Z]{2}|\d{2}-\d{2}-[A-Z]{2}|\d{2}-[A-Z]{2}-\d{2})\b', text)
    if plates:
        result["plate"] = plates[0]

    # Número apólice: "P / 1011 / 752931996   6" ou "P/1011/752931996"
    m = re.search(r'P\s*/\s*(\d+)\s*/\s*(\d+)', text)
    if m:
        result["policy_number"] = f"{m.group(1)}/{m.group(2)}"

    # Segurnet
    m = re.search(r'N[ºo°]\s*SEGURNET[:\s]*([A-Z0-9]+)', text, re.I)
    if m:
        result["segurnet"] = m.group(1)

    # Modelo do veículo — linha após a matrícula
    if result.get("plate"):
        plate = result["plate"]
        m = re.search(re.escape(plate) + r'\s*\n+\s*([A-Z][A-Z0-9 ]{5,40})', text)
        if m:
            result["vehicle_model"] = m.group(1).strip()

    # Datas: no PDF da carta verde, as datas aparecem como dígitos isolados por \n\n
    # Padrão: DD \n\n MM \n\n AAAA \n\n DD \n\n MM \n\n AAAA (início e fim lado a lado)
    # Normalizar: colapsar sequências de \n e espaços entre dígitos curtos
    # Procurar padrão: número 1-2 dígitos, newlines, número 1-2 dígitos, newlines, 4 dígitos
    date_pattern = r'(\d{1,2})\s*\n+\s*(\d{1,2})\s*\n+\s*(\d{4})'
    dates = re.findall(date_pattern, text)
    if len(dates) >= 2:
        d1, m1, y1 = dates[0]
        d2, m2, y2 = dates[1]
        try:
            result["start_date"] = dt.date(int(y1), int(m1), int(d1)).isoformat()
            result["end_date"]   = dt.date(int(y2), int(m2), int(d2)).isoformat()
        except ValueError:
            pass
    elif len(dates) == 1:
        d1, m1, y1 = dates[0]
        try:
            result["start_date"] = dt.date(int(y1), int(m1), int(d1)).isoformat()
        except ValueError:
            pass

    return result


def parse_document(text: str) -> dict:
    """Extract insurance fields from raw text using regex.
    Tries carta verde specific parser first, falls back to generic patterns."""
    # Detect carta verde (Fidelidade green card)
    is_carta_verde = bool(re.search(r'CARTE INTERNATIONALE|CARTA VERDE|INTERNATIONAL MOTOR INSURANCE', text, re.I))

    if is_carta_verde:
        result = _parse_carta_verde(text)
    else:
        result = {}
        for field, pattern in PATTERNS.items():
            m = re.search(pattern, text, re.I)
            if m:
                result[field] = m.group(1).strip()
        # Normalise dates
        for df in ("start_date", "end_date"):
            if df in result:
                result[df] = _parse_date(result[df])

    # Normalise premium
    if "premium" in result:
        try:
            result["premium"] = float(str(result["premium"]).replace(".", "").replace(",", "."))
        except (ValueError, AttributeError):
            del result["premium"]

    return result


# ── CRUD ──────────────────────────────────────────────────────────────────────

def add_policy(engine, sa_text, **fields) -> dict:
    """Insert a new insurance policy. Returns {ok, policy_id}."""
    cols = ["insurer_name", "entity_type", "entity_ref", "policy_number",
            "category", "coverage_summary", "start_date", "end_date",
            "renewal_date", "payment_frequency", "premium_amount",
            "status", "auto_renew", "notes"]
    row = {c: fields.get(c) for c in cols}
    row["insurer_name"] = row["insurer_name"] or "Desconhecida"
    row["entity_type"]  = row["entity_type"] or "company"
    row["status"]       = row["status"] or "active"
    if row.get("auto_renew") is None:
        row["auto_renew"] = True

    placeholders = ", ".join(f":{c}" for c in row)
    col_list = ", ".join(row.keys())
    sql = f"INSERT INTO public.insurance_policies ({col_list}) VALUES ({placeholders}) RETURNING id"
    with engine.begin() as conn:
        result = conn.execute(sa_text(sql), row)
        policy_id = result.fetchone()[0]
    return {"ok": True, "policy_id": policy_id}


def update_policy(engine, sa_text, policy_id: int, **fields) -> dict:
    """Update mutable fields on a policy."""
    allowed = ["entity_ref", "insurer_name", "policy_number", "category",
               "coverage_summary", "start_date", "end_date", "renewal_date",
               "payment_frequency", "premium_amount", "status", "auto_renew", "notes"]
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return {"ok": False, "error": "Nenhum campo para actualizar"}
    updates["updated_at"] = dt.datetime.now(dt.timezone.utc)
    sets = ", ".join(f"{k} = :{k}" for k in updates)
    updates["_id"] = policy_id
    with engine.begin() as conn:
        conn.execute(sa_text(f"UPDATE public.insurance_policies SET {sets} WHERE id = :_id"), updates)
    return {"ok": True, "policy_id": policy_id}


def get_policy(engine, sa_text, policy_id: int) -> dict:
    """Return policy with linked documents and alerts."""
    with engine.connect() as conn:
        pol = conn.execute(sa_text(
            "SELECT * FROM public.insurance_policies WHERE id = :id"
        ), {"id": policy_id}).fetchone()
        if not pol:
            return {}
        result = _row(pol)

        docs = conn.execute(sa_text(
            "SELECT * FROM public.insurance_documents WHERE policy_id = :id ORDER BY created_at DESC"
        ), {"id": policy_id}).fetchall()
        result["documents"] = [_row(d) for d in docs]

        alerts = conn.execute(sa_text(
            "SELECT * FROM public.insurance_alerts WHERE policy_id = :id ORDER BY trigger_date"
        ), {"id": policy_id}).fetchall()
        result["alerts"] = [_row(a) for a in alerts]
    return result


def list_policies(engine, sa_text, status=None, entity_type=None, limit=20) -> list:
    """List policies with optional filters."""
    where = ["1=1"]
    params = {}
    if status:
        where.append("status = :status")
        params["status"] = status
    if entity_type:
        where.append("entity_type = :entity_type")
        params["entity_type"] = entity_type
    params["limit"] = limit
    sql = f"""
        SELECT id, insurer_name, entity_type, entity_ref, policy_number,
               category, start_date, end_date, renewal_date,
               premium_amount, status, auto_renew, notes, created_at
        FROM public.insurance_policies
        WHERE {' AND '.join(where)}
        ORDER BY COALESCE(renewal_date, end_date) ASC NULLS LAST
        LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(sa_text(sql), params).fetchall()
    return [_row(r) for r in rows]


def add_document(engine, sa_text, policy_id: int, doc_type: str = "policy",
                 file_path: str = None, source_type: str = "manual",
                 extracted_text: str = None, issue_date=None,
                 due_date=None, amount=None, review_required=False,
                 metadata: dict = None) -> dict:
    """Attach a document to a policy."""
    sql = """
        INSERT INTO public.insurance_documents
          (policy_id, doc_type, file_path, source_type, extracted_text,
           issue_date, due_date, amount, review_required, metadata_json)
        VALUES (:policy_id, :doc_type, :file_path, :source_type, :extracted_text,
                :issue_date, :due_date, :amount, :review_required, :metadata_json)
        RETURNING id
    """
    params = dict(policy_id=policy_id, doc_type=doc_type, file_path=file_path,
                  source_type=source_type, extracted_text=extracted_text,
                  issue_date=issue_date, due_date=due_date, amount=amount,
                  review_required=review_required,
                  metadata_json=json.dumps(metadata or {}))
    with engine.begin() as conn:
        result = conn.execute(sa_text(sql), params)
        doc_id = result.fetchone()[0]
    return {"ok": True, "doc_id": doc_id}


def ingest_pdf(engine, sa_text, file_path: str) -> dict:
    """Read text from a file, parse insurance fields, store as document.
    For PDFs, requires pdfminer or pdfplumber if available; falls back to raw read."""
    text = ""
    path = Path(file_path)
    if not path.exists():
        return {"ok": False, "error": f"Ficheiro não encontrado: {file_path}"}

    if path.suffix.lower() == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(path) as pdf:
                text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        except ImportError:
            try:
                from pdfminer.high_level import extract_text as _extract
                text = _extract(str(path))
            except ImportError:
                text = path.read_bytes().decode("utf-8", errors="ignore")
    else:
        text = path.read_text(errors="ignore")

    parsed = parse_document(text)

    # Copy to runtime/insurance/
    dest = UPLOADS_DIR / path.name
    if not dest.exists():
        import shutil
        shutil.copy2(path, dest)

    # Try to match existing policy by policy_number or plate
    policy_id = None
    created_policy = False
    if parsed.get("policy_number"):
        with engine.connect() as conn:
            row = conn.execute(sa_text(
                "SELECT id FROM public.insurance_policies WHERE policy_number = :pn"
            ), {"pn": parsed["policy_number"]}).fetchone()
            if row:
                policy_id = row[0]

    if not policy_id and parsed.get("plate"):
        with engine.connect() as conn:
            row = conn.execute(sa_text(
                "SELECT id FROM public.insurance_policies WHERE entity_ref = :ref"
            ), {"ref": parsed["plate"]}).fetchone()
            if row:
                policy_id = row[0]

    # Auto-create policy if we have enough data and no match
    if not policy_id and parsed.get("insurer") and (parsed.get("end_date") or parsed.get("start_date")):
        vehicle_model = parsed.get("vehicle_model", "")
        coverage = f"Seguro automóvel — {vehicle_model}".strip(" —") if vehicle_model else "Seguro automóvel"
        pol = add_policy(engine, sa_text,
                         insurer_name=parsed.get("insurer", "Desconhecida"),
                         entity_type="vehicle",
                         entity_ref=parsed.get("plate", ""),
                         policy_number=parsed.get("policy_number", ""),
                         category="automovel",
                         coverage_summary=coverage,
                         start_date=parsed.get("start_date"),
                         end_date=parsed.get("end_date"),
                         renewal_date=parsed.get("end_date"),
                         payment_frequency="annual",
                         status="active",
                         auto_renew=True)
        policy_id = pol["policy_id"]
        created_policy = True

    doc = add_document(engine, sa_text,
                       policy_id=policy_id,
                       doc_type="policy",
                       file_path=str(dest),
                       source_type="upload",
                       extracted_text=text[:5000],
                       review_required=not created_policy,
                       metadata=parsed)
    return {"ok": True, "parsed": parsed, "doc_id": doc["doc_id"],
            "policy_id": policy_id, "created_policy": created_policy}


# ── Alert generation ──────────────────────────────────────────────────────────

ALERT_THRESHOLDS = [365, 180, 90, 60, 30, 15, 7, 1]


def generate_alerts(engine, sa_text) -> dict:
    """Create renewal/expiry alerts for active policies. Returns counts."""
    today = dt.date.today()
    created = 0
    expired = 0

    with engine.begin() as conn:
        rows = conn.execute(sa_text("""
            SELECT id, insurer_name, entity_ref, category,
                   end_date, renewal_date, status
            FROM public.insurance_policies
            WHERE status IN ('active', 'pending')
        """)).fetchall()

        for r in rows:
            pol = _row(r)
            pol_id = pol["id"]

            # Determine the relevant date (renewal > end)
            ref_date_str = pol.get("renewal_date") or pol.get("end_date")
            if not ref_date_str:
                continue
            ref_date = dt.date.fromisoformat(str(ref_date_str)[:10])

            # Mark as expired
            if ref_date < today and pol["status"] == "active":
                conn.execute(sa_text(
                    "UPDATE public.insurance_policies SET status='expired', updated_at=NOW() WHERE id=:id"
                ), {"id": pol_id})
                expired += 1
                continue

            days_left = (ref_date - today).days
            alert_type = "renewal" if pol.get("renewal_date") else "expiry"

            for threshold in ALERT_THRESHOLDS:
                if days_left <= threshold:
                    trigger_date = today.isoformat()
                    try:
                        conn.execute(sa_text("""
                            INSERT INTO public.insurance_alerts
                              (policy_id, alert_type, trigger_date)
                            VALUES (:policy_id, :alert_type, :trigger_date)
                            ON CONFLICT (policy_id, alert_type, trigger_date) DO NOTHING
                        """), dict(policy_id=pol_id, alert_type=alert_type,
                                   trigger_date=trigger_date))
                        created += 1
                    except Exception:
                        pass

    return {"created": created, "expired": expired}


# ── Stats ──────────────────────────────────────────────────────────────────────

def get_stats(engine, sa_text) -> dict:
    today = dt.date.today()
    in_30 = (today + dt.timedelta(days=30)).isoformat()

    with engine.connect() as conn:
        totals = dict(conn.execute(sa_text("""
            SELECT
              COUNT(*) FILTER (WHERE status='active')   AS active,
              COUNT(*) FILTER (WHERE status='expired')  AS expired,
              COUNT(*) FILTER (WHERE status='pending')  AS pending,
              COUNT(*) FILTER (WHERE status='cancelled') AS cancelled,
              COALESCE(SUM(premium_amount) FILTER (WHERE status='active'), 0) AS premium_total
            FROM public.insurance_policies
        """)).fetchone()._mapping)

        expiring = conn.execute(sa_text("""
            SELECT COUNT(*) AS n FROM public.insurance_policies
            WHERE status='active'
              AND COALESCE(renewal_date, end_date) BETWEEN :today AND :in30
        """), {"today": today.isoformat(), "in30": in_30}).fetchone()[0]

        pending_alerts = conn.execute(sa_text("""
            SELECT COUNT(*) AS n FROM public.insurance_alerts
            WHERE status = 'pending'
        """)).fetchone()[0]

    return {
        "active":          int(totals.get("active", 0)),
        "expired":         int(totals.get("expired", 0)),
        "pending":         int(totals.get("pending", 0)),
        "cancelled":       int(totals.get("cancelled", 0)),
        "premium_total":   float(totals.get("premium_total", 0)),
        "expiring_30d":    int(expiring),
        "alerts_pending":  int(pending_alerts),
    }


def list_alerts(engine, sa_text, status="pending", limit=20) -> list:
    with engine.connect() as conn:
        rows = conn.execute(sa_text("""
            SELECT a.*, p.insurer_name, p.entity_ref, p.category,
                   p.end_date, p.renewal_date
            FROM public.insurance_alerts a
            JOIN public.insurance_policies p ON p.id = a.policy_id
            WHERE a.status = :status
            ORDER BY a.trigger_date ASC
            LIMIT :limit
        """), {"status": status, "limit": limit}).fetchall()
    return [_row(r) for r in rows]


def resolve_alert(engine, sa_text, alert_id: int) -> dict:
    with engine.begin() as conn:
        conn.execute(sa_text("""
            UPDATE public.insurance_alerts
            SET status='resolved', resolved_at=NOW()
            WHERE id = :id
        """), {"id": alert_id})
    return {"ok": True, "alert_id": alert_id}


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    parser = argparse.ArgumentParser(description="Insurance Engine CLI")
    sub = parser.add_subparsers(dest="cmd")

    # add
    p_add = sub.add_parser("add")
    p_add.add_argument("--insurer", required=True)
    p_add.add_argument("--policy-number")
    p_add.add_argument("--entity-type", default="company")
    p_add.add_argument("--entity-ref")
    p_add.add_argument("--category")
    p_add.add_argument("--start")
    p_add.add_argument("--end")
    p_add.add_argument("--renewal")
    p_add.add_argument("--premium", type=float)
    p_add.add_argument("--frequency", default="annual")
    p_add.add_argument("--notes")
    p_add.add_argument("--status", default="active")
    p_add.add_argument("--coverage")

    # list
    p_list = sub.add_parser("list")
    p_list.add_argument("--status")
    p_list.add_argument("--type")
    p_list.add_argument("--limit", type=int, default=20)

    # get
    p_get = sub.add_parser("get")
    p_get.add_argument("id", type=int)

    # update
    p_upd = sub.add_parser("update")
    p_upd.add_argument("id", type=int)
    p_upd.add_argument("--insurer")
    p_upd.add_argument("--end")
    p_upd.add_argument("--renewal")
    p_upd.add_argument("--status")
    p_upd.add_argument("--notes")
    p_upd.add_argument("--premium", type=float)

    # add-doc
    p_doc = sub.add_parser("add-doc")
    p_doc.add_argument("policy_id", type=int)
    p_doc.add_argument("--type", default="policy", dest="doc_type")
    p_doc.add_argument("--file")
    p_doc.add_argument("--amount", type=float)
    p_doc.add_argument("--source", default="manual")

    # ingest
    p_ing = sub.add_parser("ingest")
    p_ing.add_argument("file")

    # generate-alerts
    sub.add_parser("generate-alerts")

    # stats
    sub.add_parser("stats")

    # alerts
    p_alr = sub.add_parser("alerts")
    p_alr.add_argument("--status", default="pending")
    p_alr.add_argument("--limit", type=int, default=20)

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    engine, sa_text = _conn()

    if args.cmd == "add":
        result = add_policy(engine, sa_text,
                            insurer_name=args.insurer,
                            policy_number=args.policy_number,
                            entity_type=args.entity_type,
                            entity_ref=args.entity_ref,
                            category=args.category,
                            start_date=args.start,
                            end_date=args.end,
                            renewal_date=args.renewal,
                            premium_amount=args.premium,
                            payment_frequency=args.frequency,
                            status=args.status,
                            coverage_summary=args.coverage,
                            notes=args.notes)
        print(json.dumps(result, ensure_ascii=False))

    elif args.cmd == "list":
        result = list_policies(engine, sa_text, args.status, args.type, args.limit)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif args.cmd == "get":
        result = get_policy(engine, sa_text, args.id)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif args.cmd == "update":
        result = update_policy(engine, sa_text, args.id,
                               insurer_name=args.insurer,
                               end_date=args.end,
                               renewal_date=args.renewal,
                               status=args.status,
                               notes=args.notes,
                               premium_amount=args.premium)
        print(json.dumps(result, ensure_ascii=False))

    elif args.cmd == "add-doc":
        result = add_document(engine, sa_text, args.policy_id,
                              doc_type=args.doc_type,
                              file_path=args.file,
                              amount=args.amount,
                              source_type=args.source)
        print(json.dumps(result, ensure_ascii=False))

    elif args.cmd == "ingest":
        result = ingest_pdf(engine, sa_text, args.file)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif args.cmd == "generate-alerts":
        result = generate_alerts(engine, sa_text)
        print(json.dumps(result, ensure_ascii=False))

    elif args.cmd == "stats":
        result = get_stats(engine, sa_text)
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif args.cmd == "alerts":
        result = list_alerts(engine, sa_text, args.status, args.limit)
        print(json.dumps(result, ensure_ascii=False, default=str))
