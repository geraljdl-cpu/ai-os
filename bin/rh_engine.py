#!/usr/bin/env python3
# Remover bin/ do sys.path para evitar shadowing do stdlib
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

"""
rh_engine.py — Módulo RH: pessoas, contratos, documentos

Uso:
  python3 bin/rh_engine.py list_persons
  python3 bin/rh_engine.py get_person <id>
  python3 bin/rh_engine.py upsert_extra <person_id> [--iban X] [--niss X] [--data-nascimento YYYY-MM-DD]
      [--morada X] [--data-admissao YYYY-MM-DD] [--tipo-contrato-atual X]
  python3 bin/rh_engine.py list_contracts <person_id>
  python3 bin/rh_engine.py add_contract <person_id> --tipo X --data-inicio YYYY-MM-DD
      [--data-fim YYYY-MM-DD] [--notas X] [--file-path X]
  python3 bin/rh_engine.py delete_contract <id>
  python3 bin/rh_engine.py list_documents <person_id>
  python3 bin/rh_engine.py add_document <person_id> --tipo X [--descricao X] --file-path X
  python3 bin/rh_engine.py delete_document <id>
  python3 bin/rh_engine.py get_stats
"""

import argparse
import datetime as dt
import json
import os
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
AIOS_ROOT   = Path(os.environ.get("AIOS_ROOT", Path.home() / "ai-os"))
HR_DOCS_DIR = AIOS_ROOT / "runtime" / "hr_docs"
HR_DOCS_DIR.mkdir(parents=True, exist_ok=True)

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


# ── Persons ───────────────────────────────────────────────────────────────────

def list_persons(engine, sa_text) -> list:
    """Devolve todas as pessoas com dados extra de RH (LEFT JOIN)."""
    sql = """
        SELECT p.id, p.name, p.nif, p.role, p.email, p.phone, p.status,
               p.company_id, p.entity_id, p.notes, p.created_at,
               e.iban, e.niss, e.data_nascimento, e.morada,
               e.data_admissao, e.tipo_contrato_atual, e.updated_at AS extra_updated_at
        FROM public.persons p
        LEFT JOIN public.hr_persons_extra e ON e.person_id = p.id
        ORDER BY p.name
    """
    with engine.connect() as conn:
        rows = conn.execute(sa_text(sql)).fetchall()
    return [_row(r) for r in rows]


def get_person_full(engine, sa_text, person_id: int) -> dict:
    """Devolve pessoa + extra + contratos + documentos."""
    with engine.connect() as conn:
        row = conn.execute(sa_text("""
            SELECT p.id, p.name, p.nif, p.role, p.email, p.phone, p.status,
                   p.company_id, p.entity_id, p.notes, p.created_at,
                   e.iban, e.niss, e.data_nascimento, e.morada,
                   e.data_admissao, e.tipo_contrato_atual, e.updated_at AS extra_updated_at
            FROM public.persons p
            LEFT JOIN public.hr_persons_extra e ON e.person_id = p.id
            WHERE p.id = :id
        """), {"id": person_id}).fetchone()
        if not row:
            return {}
        result = _row(row)

        contracts = conn.execute(sa_text("""
            SELECT * FROM public.hr_contracts
            WHERE person_id = :id ORDER BY data_inicio DESC
        """), {"id": person_id}).fetchall()
        result["contracts"] = [_row(c) for c in contracts]

        docs = conn.execute(sa_text("""
            SELECT * FROM public.hr_documents
            WHERE person_id = :id ORDER BY uploaded_at DESC
        """), {"id": person_id}).fetchall()
        result["documents"] = [_row(d) for d in docs]

    return result


def upsert_person_extra(engine, sa_text, person_id: int, **fields) -> dict:
    """INSERT ON CONFLICT UPDATE para hr_persons_extra."""
    allowed = ["iban", "niss", "data_nascimento", "morada",
               "data_admissao", "tipo_contrato_atual"]
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return {"ok": False, "error": "Nenhum campo para actualizar"}

    updates["person_id"] = person_id
    updates["updated_at"] = dt.datetime.now(dt.timezone.utc)

    cols = list(updates.keys())
    vals = ", ".join(f":{c}" for c in cols)
    col_list = ", ".join(cols)
    set_clause = ", ".join(
        f"{c} = :{c}" for c in cols if c not in ("person_id",)
    )
    sql = f"""
        INSERT INTO public.hr_persons_extra ({col_list})
        VALUES ({vals})
        ON CONFLICT (person_id) DO UPDATE SET {set_clause}
    """
    with engine.begin() as conn:
        conn.execute(sa_text(sql), updates)
    return {"ok": True, "person_id": person_id}


# ── Contracts ─────────────────────────────────────────────────────────────────

def list_contracts(engine, sa_text, person_id: int) -> list:
    with engine.connect() as conn:
        rows = conn.execute(sa_text("""
            SELECT * FROM public.hr_contracts
            WHERE person_id = :id ORDER BY data_inicio DESC
        """), {"id": person_id}).fetchall()
    return [_row(r) for r in rows]


def add_contract(engine, sa_text, person_id: int, tipo: str,
                 data_inicio: str, data_fim: str = None,
                 notas: str = None, file_path: str = None) -> dict:
    VALID_TIPOS = ("trabalho_prazo", "trabalho_sem_termo", "recibo_verde", "nda")
    if tipo not in VALID_TIPOS:
        raise ValueError(f"tipo inválido: {tipo}. Use: {', '.join(VALID_TIPOS)}")

    params = dict(person_id=person_id, tipo=tipo, data_inicio=data_inicio,
                  data_fim=data_fim or None, notas=notas or None,
                  file_path=file_path or None)
    with engine.begin() as conn:
        result = conn.execute(sa_text("""
            INSERT INTO public.hr_contracts
              (person_id, tipo, data_inicio, data_fim, notas, file_path)
            VALUES (:person_id, :tipo, :data_inicio, :data_fim, :notas, :file_path)
            RETURNING id
        """), params)
        contract_id = result.fetchone()[0]
        # Actualizar tipo_contrato_atual na extra
        conn.execute(sa_text("""
            INSERT INTO public.hr_persons_extra (person_id, tipo_contrato_atual, updated_at)
            VALUES (:pid, :tipo, NOW())
            ON CONFLICT (person_id) DO UPDATE
              SET tipo_contrato_atual = :tipo, updated_at = NOW()
        """), {"pid": person_id, "tipo": tipo})
    return {"ok": True, "contract_id": contract_id}


def delete_contract(engine, sa_text, contract_id: int) -> dict:
    with engine.begin() as conn:
        conn.execute(sa_text(
            "DELETE FROM public.hr_contracts WHERE id = :id"
        ), {"id": contract_id})
    return {"ok": True, "contract_id": contract_id}


# ── Documents ─────────────────────────────────────────────────────────────────

def list_documents(engine, sa_text, person_id: int) -> list:
    with engine.connect() as conn:
        rows = conn.execute(sa_text("""
            SELECT * FROM public.hr_documents
            WHERE person_id = :id ORDER BY uploaded_at DESC
        """), {"id": person_id}).fetchall()
    return [_row(r) for r in rows]


def add_document(engine, sa_text, person_id: int, tipo: str,
                 file_path: str, descricao: str = None) -> dict:
    VALID_TIPOS = ("contrato", "id", "iban", "certificado", "outro")
    if tipo not in VALID_TIPOS:
        raise ValueError(f"tipo inválido: {tipo}. Use: {', '.join(VALID_TIPOS)}")

    params = dict(person_id=person_id, tipo=tipo,
                  file_path=file_path, descricao=descricao or None)
    with engine.begin() as conn:
        result = conn.execute(sa_text("""
            INSERT INTO public.hr_documents (person_id, tipo, file_path, descricao)
            VALUES (:person_id, :tipo, :file_path, :descricao)
            RETURNING id
        """), params)
        doc_id = result.fetchone()[0]
    return {"ok": True, "doc_id": doc_id}


def delete_document(engine, sa_text, doc_id: int) -> dict:
    with engine.begin() as conn:
        # Obter file_path para possível remoção futura
        row = conn.execute(sa_text(
            "SELECT file_path FROM public.hr_documents WHERE id = :id"
        ), {"id": doc_id}).fetchone()
        conn.execute(sa_text(
            "DELETE FROM public.hr_documents WHERE id = :id"
        ), {"id": doc_id})
    file_path = row[0] if row else None
    return {"ok": True, "doc_id": doc_id, "file_path": file_path}


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats(engine, sa_text) -> dict:
    today = dt.date.today()
    in_30 = (today + dt.timedelta(days=30)).isoformat()

    with engine.connect() as conn:
        persons_total = conn.execute(sa_text(
            "SELECT COUNT(*) FROM public.persons WHERE status != 'inactive'"
        )).fetchone()[0]

        contracts_expiring = conn.execute(sa_text("""
            SELECT COUNT(*) FROM public.hr_contracts
            WHERE data_fim IS NOT NULL
              AND data_fim BETWEEN :today AND :in30
        """), {"today": today.isoformat(), "in30": in_30}).fetchone()[0]

        # Pessoas activas sem nenhum documento
        docs_missing = conn.execute(sa_text("""
            SELECT COUNT(*) FROM public.persons p
            WHERE p.status != 'inactive'
              AND NOT EXISTS (
                SELECT 1 FROM public.hr_documents d WHERE d.person_id = p.id
              )
        """)).fetchone()[0]

    return {
        "persons_total":         int(persons_total),
        "contracts_expiring_30d": int(contracts_expiring),
        "docs_missing":          int(docs_missing),
        "badge_count":           int(contracts_expiring) + int(docs_missing),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    parser = argparse.ArgumentParser(description="RH Engine CLI")
    sub = parser.add_subparsers(dest="cmd")

    # list_persons
    sub.add_parser("list_persons")

    # get_person
    p_get = sub.add_parser("get_person")
    p_get.add_argument("id", type=int)

    # upsert_extra
    p_ue = sub.add_parser("upsert_extra")
    p_ue.add_argument("person_id", type=int)
    p_ue.add_argument("--iban")
    p_ue.add_argument("--niss")
    p_ue.add_argument("--data-nascimento", dest="data_nascimento")
    p_ue.add_argument("--morada")
    p_ue.add_argument("--data-admissao", dest="data_admissao")
    p_ue.add_argument("--tipo-contrato-atual", dest="tipo_contrato_atual")

    # list_contracts
    p_lc = sub.add_parser("list_contracts")
    p_lc.add_argument("person_id", type=int)

    # add_contract
    p_ac = sub.add_parser("add_contract")
    p_ac.add_argument("person_id", type=int)
    p_ac.add_argument("--tipo", required=True)
    p_ac.add_argument("--data-inicio", dest="data_inicio", required=True)
    p_ac.add_argument("--data-fim", dest="data_fim")
    p_ac.add_argument("--notas")
    p_ac.add_argument("--file-path", dest="file_path")

    # delete_contract
    p_dc = sub.add_parser("delete_contract")
    p_dc.add_argument("id", type=int)

    # list_documents
    p_ld = sub.add_parser("list_documents")
    p_ld.add_argument("person_id", type=int)

    # add_document
    p_ad = sub.add_parser("add_document")
    p_ad.add_argument("person_id", type=int)
    p_ad.add_argument("--tipo", required=True)
    p_ad.add_argument("--file-path", dest="file_path", required=True)
    p_ad.add_argument("--descricao")

    # delete_document
    p_dd = sub.add_parser("delete_document")
    p_dd.add_argument("id", type=int)

    # get_stats
    sub.add_parser("get_stats")

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        sys.exit(1)

    engine, sa_text = _conn()

    if args.cmd == "list_persons":
        print(json.dumps(list_persons(engine, sa_text), ensure_ascii=False, default=str))

    elif args.cmd == "get_person":
        print(json.dumps(get_person_full(engine, sa_text, args.id), ensure_ascii=False, default=str))

    elif args.cmd == "upsert_extra":
        fields = {}
        for f in ("iban", "niss", "data_nascimento", "morada", "data_admissao", "tipo_contrato_atual"):
            v = getattr(args, f, None)
            if v is not None:
                fields[f] = v
        print(json.dumps(upsert_person_extra(engine, sa_text, args.person_id, **fields), ensure_ascii=False))

    elif args.cmd == "list_contracts":
        print(json.dumps(list_contracts(engine, sa_text, args.person_id), ensure_ascii=False, default=str))

    elif args.cmd == "add_contract":
        print(json.dumps(add_contract(engine, sa_text, args.person_id,
                                      args.tipo, args.data_inicio,
                                      args.data_fim, args.notas, args.file_path),
                         ensure_ascii=False))

    elif args.cmd == "delete_contract":
        print(json.dumps(delete_contract(engine, sa_text, args.id), ensure_ascii=False))

    elif args.cmd == "list_documents":
        print(json.dumps(list_documents(engine, sa_text, args.person_id), ensure_ascii=False, default=str))

    elif args.cmd == "add_document":
        print(json.dumps(add_document(engine, sa_text, args.person_id,
                                      args.tipo, args.file_path, args.descricao),
                         ensure_ascii=False))

    elif args.cmd == "delete_document":
        print(json.dumps(delete_document(engine, sa_text, args.id), ensure_ascii=False))

    elif args.cmd == "get_stats":
        print(json.dumps(get_stats(engine, sa_text), ensure_ascii=False))
