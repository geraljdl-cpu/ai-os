#!/usr/bin/env python3
"""
reconcile.py — Motor de reconciliação bancária.

Funções:
  parse_csv(filepath)  — parseia CSV bancário PT (BPI, CGD, Millennium, Santander)
  auto_match(conn)     — match automático por valor + referência + NIF
  insert_transactions  — insere movimentos novos
  Comandos CLI: bank_import <file>, bank_transactions, bank_reconcile, bank_match <tx_id> <inv_id>
"""
import csv
import io
import json
import os
import sys
from datetime import datetime, timezone

# --- evitar shadow de stdlib ---
_bin_dir = os.path.dirname(os.path.abspath(__file__))
if _bin_dir in sys.path:
    sys.path.remove(_bin_dir)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _row(r) -> dict:
    import datetime as _dt
    from decimal import Decimal as _Dec
    d = dict(r)
    for k, v in d.items():
        if isinstance(v, _dt.datetime):
            d[k] = v.isoformat()
        elif isinstance(v, _dt.date):
            d[k] = v.isoformat()
        elif isinstance(v, _Dec):
            d[k] = float(v)
    return d


# ── CSV parser (formato PT) ────────────────────────────────────────────────────

_DATE_FMTS = ["%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y"]

def _parse_date(s: str):
    s = s.strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def _parse_amount(s: str) -> float | None:
    """Normaliza '1.234,56' ou '1234.56' para float."""
    s = s.strip().replace(" ", "")
    if not s or s in ("-", ""):
        return None
    # PT format: 1.234,56
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_csv(content: str) -> list[dict]:
    """
    Parseia CSV bancário PT genérico.
    Detecta automaticamente separador (;  ,  \t).
    Detecta colunas: data, descrição, débito, crédito, valor, referência, nif.
    Devolve lista de dicts: {date, amount, description, reference, nif}.
    amount > 0 → crédito (dinheiro que entra); < 0 → débito.
    """
    # Detectar separador
    sample = content[:2000]
    sep = ";"
    for s in (";", "\t", ","):
        if sample.count(s) > sample.count(sep):
            sep = s

    reader = csv.DictReader(io.StringIO(content), delimiter=sep)
    rows = []
    for raw in reader:
        # Normalizar nomes de colunas
        r = {k.strip().lower(): v for k, v in raw.items() if k}

        # Data
        date_val = None
        for col in ("data", "date", "data mov", "data valor", "data_mov"):
            if col in r and r[col].strip():
                date_val = _parse_date(r[col])
                break
        if not date_val:
            continue

        # Valor — preferir coluna "valor" ou calcular débito/crédito
        amount = None
        for col in ("valor", "montante", "amount", "importe"):
            if col in r and r[col].strip():
                amount = _parse_amount(r[col])
                break
        if amount is None:
            credit = _parse_amount(r.get("credito", r.get("crédito", r.get("credit", ""))) or "")
            debit  = _parse_amount(r.get("debito",  r.get("débito",  r.get("debit",  ""))) or "")
            if credit is not None and credit != 0:
                amount = credit
            elif debit is not None and debit != 0:
                amount = -abs(debit)
        if amount is None:
            continue

        # Descrição
        desc = ""
        for col in ("descricao", "descrição", "description", "descritivo", "observacoes", "observações"):
            if col in r and r[col].strip():
                desc = r[col].strip()
                break
        if not desc:
            # pegar o campo mais longo como descrição
            longest = max(r.values(), key=lambda v: len(v.strip()), default="")
            desc = longest.strip()

        # Referência
        ref = ""
        for col in ("referencia", "referência", "reference", "ref", "doc", "nº doc"):
            if col in r and r[col].strip():
                ref = r[col].strip()
                break

        # NIF
        nif = ""
        for col in ("nif", "contribuinte", "vat", "nif ordenante", "nif beneficiario"):
            if col in r and r[col].strip():
                nif = r[col].strip()
                break

        rows.append({
            "date":        date_val.isoformat(),
            "amount":      round(amount, 2),
            "description": desc[:500],
            "reference":   ref[:100],
            "nif":         nif[:20],
        })

    return rows


# ── Inserir transacções ────────────────────────────────────────────────────────

def insert_transactions(conn, rows: list[dict]) -> int:
    """Insere movimentos; ignora duplicados (date + amount + description)."""
    from sqlalchemy import text
    inserted = 0
    for r in rows:
        result = conn.execute(text("""
            INSERT INTO public.bank_transactions (date, amount, description, reference, nif)
            VALUES (:date, :amount, :description, :reference, :nif)
            ON CONFLICT DO NOTHING
            RETURNING id
        """), r)
        if result.rowcount:
            inserted += 1
    return inserted


# ── Auto-match ─────────────────────────────────────────────────────────────────

def auto_match(conn) -> list[dict]:
    """
    Tenta fazer match entre movimentos bank_transactions (unmatched, amount>0)
    e faturas twin_invoices (status='issued' ou 'overdue').

    Lógica por ordem de prioridade:
      1. amount == invoice.amount (tolerância ±0.02) + referência coincide com número fatura
      2. amount == invoice.amount + NIF coincide com cliente NIF
      3. amount == invoice.amount (exact match, só 1 candidato)

    Marca matched e insere bank_reconciliation.
    """
    from sqlalchemy import text
    matches = []

    # Buscar movimentos de crédito não reconciliados
    txs = conn.execute(text("""
        SELECT id, date, amount, description, reference, nif
        FROM public.bank_transactions
        WHERE status = 'unmatched' AND amount > 0
        ORDER BY date DESC
    """)).mappings().all()

    # Buscar faturas abertas
    invs = conn.execute(text("""
        SELECT i.id, i.number, i.amount, i.client,
               e.metadata->>'nif' AS client_nif
        FROM public.twin_invoices i
        LEFT JOIN public.twin_entities e ON e.id = i.entity_id
        WHERE i.status IN ('issued','overdue')
    """)).mappings().all()

    inv_map_amount = {}  # amount → [inv, ...]
    for inv in invs:
        key = float(inv["amount"])
        inv_map_amount.setdefault(key, []).append(dict(inv))

    for tx in txs:
        tx_amount = float(tx["amount"])
        matched_inv = None
        match_type  = "auto"
        confidence  = 0

        # Candidatos com valor próximo (±0.02)
        candidates = []
        for amt_key, inv_list in inv_map_amount.items():
            if abs(amt_key - tx_amount) <= 0.02:
                candidates.extend(inv_list)

        if not candidates:
            continue

        # Prioridade 1: número de fatura na referência ou descrição
        tx_ref  = (tx["reference"] or "").upper()
        tx_desc = (tx["description"] or "").upper()
        for inv in candidates:
            inv_num = (inv["number"] or "").upper()
            if inv_num and (inv_num in tx_ref or inv_num in tx_desc):
                matched_inv = inv
                confidence  = 95
                break

        # Prioridade 2: NIF coincide
        if not matched_inv and tx["nif"]:
            for inv in candidates:
                if inv.get("client_nif") and inv["client_nif"] == tx["nif"]:
                    matched_inv = inv
                    confidence  = 80
                    break

        # Prioridade 3: valor exact e só 1 candidato
        if not matched_inv and len(candidates) == 1 and abs(float(candidates[0]["amount"]) - tx_amount) < 0.01:
            matched_inv = candidates[0]
            confidence  = 60

        if matched_inv and confidence >= 60:
            inv_id = matched_inv["id"]
            # Actualizar transacção
            conn.execute(text("""
                UPDATE public.bank_transactions
                SET status = 'matched', matched_invoice_id = :inv_id
                WHERE id = :tx_id
            """), {"inv_id": inv_id, "tx_id": tx["id"]})
            # Marcar fatura como paga
            conn.execute(text("""
                UPDATE public.twin_invoices
                SET status = 'paid', paid_at = NOW(), updated_at = NOW()
                WHERE id = :inv_id AND status != 'paid'
            """), {"inv_id": inv_id})
            # Registar reconciliação
            conn.execute(text("""
                INSERT INTO public.bank_reconciliation (transaction_id, invoice_id, match_type, confidence)
                VALUES (:tx_id, :inv_id, :mt, :conf)
                ON CONFLICT DO NOTHING
            """), {"tx_id": tx["id"], "inv_id": inv_id, "mt": match_type, "conf": confidence})
            # Evento
            conn.execute(text("""
                INSERT INTO public.events (ts, level, source, kind, message, data)
                VALUES (NOW(),'info','bank','invoice_paid',:msg,:data)
            """), {
                "msg":  f"Fatura {matched_inv['number']} reconciliada automaticamente (confiança {confidence}%)",
                "data": json.dumps({"transaction_id": tx["id"], "invoice_id": inv_id, "confidence": confidence}),
            })
            # Remover do mapa para não fazer double-match
            amt_key = float(matched_inv["amount"])
            if amt_key in inv_map_amount:
                inv_map_amount[amt_key] = [i for i in inv_map_amount[amt_key] if i["id"] != inv_id]

            matches.append({"transaction_id": tx["id"], "invoice_id": inv_id, "confidence": confidence})

    return matches


# ── CLI commands ───────────────────────────────────────────────────────────────

def cmd_bank_import(args):
    """Importar CSV bancário. Args: <filepath>"""
    if not args:
        print(json.dumps({"error": "usage: bank_import <filepath>"}))
        return
    filepath = args[0]
    if not os.path.exists(filepath):
        print(json.dumps({"error": f"ficheiro não encontrado: {filepath}"}))
        return
    with open(filepath, encoding="utf-8-sig", errors="replace") as f:
        content = f.read()
    rows = parse_csv(content)
    if not rows:
        print(json.dumps({"ok": False, "error": "sem linhas válidas no CSV", "parsed": 0}))
        return
    engine, _ = _conn()
    with engine.begin() as conn:
        inserted = insert_transactions(conn, rows)
    print(json.dumps({"ok": True, "parsed": len(rows), "inserted": inserted}))


def cmd_bank_transactions(args):
    """Lista movimentos. Args: [limit=100] [status=all]"""
    limit  = int(args[0]) if args and args[0].isdigit() else 100
    status = args[1] if len(args) > 1 and args[1] in ("unmatched","matched","ignored") else None
    engine, text = _conn()
    with engine.connect() as c:
        if status:
            rows = c.execute(text("""
                SELECT bt.id, bt.date, bt.amount, bt.description, bt.reference, bt.nif,
                       bt.status, bt.matched_invoice_id, bt.imported_at,
                       ti.number AS invoice_number
                FROM public.bank_transactions bt
                LEFT JOIN public.twin_invoices ti ON ti.id = bt.matched_invoice_id
                WHERE bt.status = :s
                ORDER BY bt.date DESC, bt.id DESC LIMIT :l
            """), {"s": status, "l": limit}).mappings().all()
        else:
            rows = c.execute(text("""
                SELECT bt.id, bt.date, bt.amount, bt.description, bt.reference, bt.nif,
                       bt.status, bt.matched_invoice_id, bt.imported_at,
                       ti.number AS invoice_number
                FROM public.bank_transactions bt
                LEFT JOIN public.twin_invoices ti ON ti.id = bt.matched_invoice_id
                ORDER BY bt.date DESC, bt.id DESC LIMIT :l
            """), {"l": limit}).mappings().all()

    summary = {"total": 0, "matched": 0, "unmatched": 0, "ignored": 0}
    txs = []
    for r in rows:
        d = _row(r)
        txs.append(d)
        summary["total"] += 1
        summary[d.get("status", "unmatched")] = summary.get(d.get("status","unmatched"), 0) + 1

    print(json.dumps({"ok": True, "transactions": txs, "summary": summary}, ensure_ascii=False))


def cmd_bank_reconcile(_args):
    """Executar auto-match em todos os movimentos unmatched."""
    engine, _ = _conn()
    with engine.begin() as conn:
        matches = auto_match(conn)
    print(json.dumps({"ok": True, "matched": len(matches), "details": matches}))


def cmd_bank_match(args):
    """Match manual. Args: <transaction_id> <invoice_id>"""
    if len(args) < 2:
        print(json.dumps({"error": "usage: bank_match <transaction_id> <invoice_id>"}))
        return
    tx_id  = int(args[0])
    inv_id = int(args[1])
    engine, text = _conn()
    with engine.begin() as c:
        # Validar existência
        tx  = c.execute(text("SELECT id, amount, status FROM public.bank_transactions WHERE id=:id"), {"id": tx_id}).mappings().first()
        inv = c.execute(text("SELECT id, number, status FROM public.twin_invoices WHERE id=:id"), {"id": inv_id}).mappings().first()
        if not tx:
            print(json.dumps({"error": f"transacção #{tx_id} não encontrada"})); return
        if not inv:
            print(json.dumps({"error": f"fatura #{inv_id} não encontrada"})); return

        c.execute(text("""
            UPDATE public.bank_transactions
            SET status='matched', matched_invoice_id=:inv_id WHERE id=:tx_id
        """), {"inv_id": inv_id, "tx_id": tx_id})
        c.execute(text("""
            UPDATE public.twin_invoices SET status='paid', paid_at=NOW(), updated_at=NOW()
            WHERE id=:inv_id AND status != 'paid'
        """), {"inv_id": inv_id})
        c.execute(text("""
            INSERT INTO public.bank_reconciliation (transaction_id, invoice_id, match_type, confidence)
            VALUES (:tx, :inv, 'manual', 100)
            ON CONFLICT DO NOTHING
        """), {"tx": tx_id, "inv": inv_id})
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(),'info','bank','invoice_paid_manual',:msg,:data)
        """), {
            "msg":  f"Fatura {inv['number']} reconciliada manualmente",
            "data": json.dumps({"transaction_id": tx_id, "invoice_id": inv_id}),
        })
    print(json.dumps({"ok": True, "transaction_id": tx_id, "invoice_id": inv_id, "invoice_number": inv["number"]}))


def cmd_bank_ignore(args):
    """Marcar movimento como ignorado. Args: <transaction_id>"""
    if not args:
        print(json.dumps({"error": "usage: bank_ignore <transaction_id>"})); return
    tx_id = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        c.execute(text("UPDATE public.bank_transactions SET status='ignored' WHERE id=:id"), {"id": tx_id})
    print(json.dumps({"ok": True, "transaction_id": tx_id}))


if __name__ == "__main__":
    CMDS = {
        "bank_import":      cmd_bank_import,
        "bank_transactions": cmd_bank_transactions,
        "bank_reconcile":   cmd_bank_reconcile,
        "bank_match":       cmd_bank_match,
        "bank_ignore":      cmd_bank_ignore,
    }
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(f"usage: reconcile.py <{'|'.join(CMDS)}>", file=sys.stderr)
        sys.exit(1)
    try:
        CMDS[sys.argv[1]](sys.argv[2:])
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": str(e)}))
