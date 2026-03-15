#!/usr/bin/env python3
"""
invoice_engine.py — motor de faturação do AI-OS.

Fluxo:
  timesheets aprovadas → agrupar por evento/cliente → criar twin_invoice (draft)
  → opcionalmente push para Toconline (finalize=0 sempre — nunca emite sem confirmação humana)

Uso:
  python3 bin/invoice_engine.py generate_from_timesheets <event_name> [client_id]
  python3 bin/invoice_engine.py push_to_toconline <invoice_id>
  python3 bin/invoice_engine.py list_drafts
  python3 bin/invoice_engine.py status
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import os
import sys
import urllib.request
import urllib.error
from datetime import datetime, date, timedelta

import sqlalchemy as sa

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
TOC_TOKEN_FILE = os.path.expanduser("~/ai-os/.toc_token.json")
TOC_BASE_URL   = "https://api29.toconline.pt"
INVOICE_PREFIX = "AIOS"


def _conn():
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _toc_token() -> str:
    try:
        return json.load(open(TOC_TOKEN_FILE))["access_token"]
    except Exception:
        return ""


def _toc_get(path: str) -> dict:
    token = _toc_token()
    if not token:
        return {"ok": False, "error": "Token Toconline não configurado"}
    req = urllib.request.Request(
        TOC_BASE_URL + path,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"}
    )
    try:
        r = urllib.request.urlopen(req, timeout=15)
        return {"ok": True, "data": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"ok": False, "status": e.code, "error": body[:200]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _toc_post(path: str, body: dict) -> dict:
    token = _toc_token()
    if not token:
        return {"ok": False, "error": "Token Toconline não configurado"}
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        TOC_BASE_URL + path, data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Accept": "application/json"}
    )
    try:
        r = urllib.request.urlopen(req, timeout=20)
        return {"ok": True, "data": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"ok": False, "status": e.code, "error": body[:300]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ── número de fatura ──────────────────────────────────────────────────────────

def _next_invoice_number(c, text) -> str:
    yr  = datetime.now().year
    cnt = c.execute(text(
        "SELECT COUNT(*) FROM public.twin_invoices WHERE number LIKE :pat"
    ), {"pat": f"{INVOICE_PREFIX}-{yr}-%"}).scalar()
    return f"{INVOICE_PREFIX}-{yr}-{cnt + 1:04d}"


# ── gerar invoice a partir de timesheets aprovadas ───────────────────────────

def generate_from_timesheets(event_name: str, client_id: int = None) -> dict:
    """
    Agrega timesheets aprovadas para event_name que ainda não foram faturadas.
    Cria uma twin_invoice em estado draft.
    """
    engine, text = _conn()
    with engine.begin() as c:
        # timesheets aprovadas para este evento, ainda não pagas
        rows = c.execute(text("""
            SELECT t.id, t.worker_id, t.hours, t.hourly_rate, t.notes,
                   COALESCE(p.hourly_rate, t.hourly_rate, 0) AS effective_rate
            FROM public.event_timesheets t
            LEFT JOIN public.people p ON LOWER(p.name) = LOWER(t.worker_id)
            WHERE LOWER(t.event_name) = LOWER(:ev)
              AND t.status = 'approved'
        """), {"ev": event_name}).mappings().all()

        if not rows:
            return {"ok": False, "error": f"Sem timesheets aprovadas para evento '{event_name}'"}

        # calcular total
        lines = []
        total = 0.0
        for r in rows:
            hrs   = float(r["hours"] or 0)
            rate  = float(r["effective_rate"] or 0)
            amount = round(hrs * rate, 2)
            total += amount
            lines.append({
                "worker_id":   r["worker_id"],
                "hours":       hrs,
                "hourly_rate": rate,
                "amount":      amount,
                "notes":       r["notes"] or "",
                "timesheet_id": r["id"],
            })

        # cliente (da tabela clients se fornecido, ou genérico)
        client_name = None
        if client_id:
            cl = c.execute(text(
                "SELECT company_name FROM public.clients WHERE id=:id"
            ), {"id": client_id}).mappings().first()
            client_name = cl["company_name"] if cl else None

        # verificar se já existe draft para este evento
        existing = c.execute(text("""
            SELECT id, number FROM public.twin_invoices
            WHERE metadata->>'event_name' = :ev AND status = 'draft'
            LIMIT 1
        """), {"ev": event_name}).mappings().first()
        if existing:
            return {"ok": True, "invoice_id": existing["id"], "number": existing["number"],
                    "already_exists": True, "total": total}

        number   = _next_invoice_number(c, text)
        due_date = (date.today() + timedelta(days=30))

        row = c.execute(text("""
            INSERT INTO public.twin_invoices
              (number, status, amount, client, due_date, metadata)
            VALUES (:num, 'draft', :amt, :cli, :due, CAST(:meta AS jsonb))
            RETURNING id
        """), {
            "num":  number,
            "amt":  total,
            "cli":  client_name or event_name,
            "due":  due_date,
            "meta": json.dumps({
                "event_name":  event_name,
                "client_id":   client_id,
                "lines":       lines,
                "generated_at": datetime.now().isoformat(),
            })
        }).mappings().first()

        inv_id = row["id"]

        # evento
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'invoice_engine', 'invoice_created',
                    :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Fatura draft {number} criada — {total:.2f}€ ({event_name})",
            "data": json.dumps({"invoice_id": inv_id, "number": number,
                                "total": total, "event_name": event_name})
        })

    return {"ok": True, "invoice_id": inv_id, "number": number,
            "total": total, "event_name": event_name, "lines": len(lines),
            "status": "draft"}


# ── push para Toconline (finalize=0 SEMPRE) ──────────────────────────────────

def push_to_toconline(invoice_id: int) -> dict:
    """
    Envia invoice draft para Toconline como RASCUNHO (finalize=0).
    Nunca emite fatura real — é sempre draft no Toconline.
    Requer confirmação humana no portal Toconline para finalizar.
    """
    engine, text = _conn()
    with engine.connect() as c:
        inv = c.execute(text("""
            SELECT id, number, amount, client, metadata
            FROM public.twin_invoices WHERE id = :id
        """), {"id": invoice_id}).mappings().first()

    if not inv:
        return {"ok": False, "error": "Invoice não encontrada"}

    meta  = inv["metadata"] if isinstance(inv["metadata"], dict) else json.loads(inv["metadata"] or "{}")
    lines = meta.get("lines", [])

    if not lines:
        return {"ok": False, "error": "Invoice sem linhas — não é possível enviar"}

    # Toconline: primeiro obter/criar cliente
    client_id_toc = meta.get("toconline_customer_id")
    if not client_id_toc:
        # tentar encontrar por nome
        r_cust = _toc_get(f"/api/customers?filter[search]={urllib.request.quote(str(inv['client'] or ''))}&page[size]=1")
        if r_cust.get("ok"):
            data = r_cust["data"]
            items = data.get("data", data) if isinstance(data, dict) else data
            if items:
                client_id_toc = items[0].get("id")

    if not client_id_toc:
        return {"ok": False, "error": "Cliente não encontrado no Toconline. Criar primeiro em /finance."}

    # construir linhas do documento
    doc_lines = []
    for l in lines:
        desc = f"{l['worker_id']} — {l['hours']}h @ {l['hourly_rate']}€/h"
        if l.get("notes"):
            desc += f" ({l['notes']})"
        doc_lines.append({
            "description":  desc,
            "quantity":     l["hours"],
            "unit_price":   l["hourly_rate"],
        })

    body = {
        "document_type": "FT",
        "date":          date.today().isoformat(),
        "finalize":      0,          # NUNCA finalizar automaticamente
        "customer_id":   int(client_id_toc),
        "notes":         f"Ref: {inv['number']} — gerado pelo AI-OS",
        "lines":         doc_lines,
    }

    r = _toc_post("/api/v1/commercial_sales_documents", body)
    if not r.get("ok"):
        return {"ok": False, "error": f"Toconline: {r.get('error','desconhecido')}",
                "toc_status": r.get("status")}

    toc_id = r["data"].get("id") or r["data"].get("data", {}).get("id")

    # guardar referência Toconline na invoice
    with engine.begin() as c:
        c.execute(text("""
            UPDATE public.twin_invoices
            SET metadata = metadata || CAST(:patch AS jsonb), updated_at = NOW()
            WHERE id = :id
        """), {
            "patch": json.dumps({"toconline_id": toc_id, "toconline_pushed_at": datetime.now().isoformat(),
                                  "toconline_customer_id": client_id_toc}),
            "id": invoice_id
        })
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'invoice_engine', 'invoice_sent_toconline',
                    :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Invoice {inv['number']} enviada para Toconline (draft #{toc_id})",
            "data": json.dumps({"invoice_id": invoice_id, "toconline_id": toc_id})
        })

    return {"ok": True, "invoice_id": invoice_id, "toconline_id": toc_id,
            "status": "draft_in_toconline",
            "note": "Documento criado como RASCUNHO no Toconline. Para emitir, finalizar manualmente no portal."}


# ── listar drafts ─────────────────────────────────────────────────────────────

def list_drafts() -> dict:
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, number, amount, client, due_date, metadata, created_at
            FROM public.twin_invoices
            WHERE status = 'draft'
            ORDER BY created_at DESC LIMIT 30
        """)).mappings().all()
    items = []
    for r in rows:
        meta = r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"] or "{}")
        items.append({
            "id": r["id"], "number": r["number"], "amount": float(r["amount"]),
            "client": r["client"], "due_date": str(r["due_date"]),
            "event_name": meta.get("event_name"),
            "lines": len(meta.get("lines", [])),
            "toconline_id": meta.get("toconline_id"),
            "created_at": r["created_at"].isoformat(),
        })
    return {"ok": True, "drafts": items}


# ── status Toconline ──────────────────────────────────────────────────────────

def toconline_status() -> dict:
    r = _toc_get("/api/customers?page[size]=1")
    if r.get("ok"):
        return {"ok": True, "toconline": "connected", "base_url": TOC_BASE_URL}
    return {"ok": False, "toconline": "error", "error": r.get("error", ""),
            "hint": "Token expirado? Fazer login no Toconline e actualizar .toc_token.json"}


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "generate_from_timesheets":
        if len(sys.argv) < 3:
            print('{"error": "event_name obrigatório"}'); sys.exit(1)
        event_name = sys.argv[2]
        client_id  = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print(json.dumps(generate_from_timesheets(event_name, client_id), ensure_ascii=False))

    elif cmd == "push_to_toconline":
        if len(sys.argv) < 3:
            print('{"error": "invoice_id obrigatório"}'); sys.exit(1)
        print(json.dumps(push_to_toconline(int(sys.argv[2])), ensure_ascii=False))

    elif cmd == "list_drafts":
        print(json.dumps(list_drafts(), ensure_ascii=False))

    elif cmd == "status":
        print(json.dumps(toconline_status(), ensure_ascii=False))

    else:
        print(f'{{"error": "cmd desconhecido: {cmd}"}}')
        sys.exit(1)


if __name__ == "__main__":
    main()
