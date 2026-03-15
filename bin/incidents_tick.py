#!/usr/bin/env python3
"""
incidents_tick.py — Auto-detecção de incidentes.

Fontes:
  - workers offline > 5min
  - tarefas twin_tasks bloqueadas > 24h
  - obrigações fiscais vencidas ou < 5 dias
  - tenders com deadline ≤ 3 dias
  - API externa falhou (Toconline health)
  - movimentos bancários sem match > 72h

Corre via systemd timer aios-incidents.timer a cada 5min.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta

_bin_dir = os.path.dirname(os.path.abspath(__file__))
if _bin_dir in sys.path:
    sys.path.remove(_bin_dir)

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)

_ROOT = os.path.dirname(_bin_dir)


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _ensure_incident(conn, text, source: str, kind: str, severity: str, title: str, details: str = None):
    """Cria incidente se não existe nenhum open com mesmo source+kind nas últimas 4h."""
    existing = conn.execute(text("""
        SELECT id FROM public.incidents
        WHERE source=:s AND kind=:k AND status='open'
          AND created_at > NOW() - INTERVAL '4 hours'
        LIMIT 1
    """), {"s": source, "k": kind}).mappings().first()
    if existing:
        return False  # já existe, nada a fazer
    conn.execute(text("""
        INSERT INTO public.incidents (source, kind, severity, title, details)
        VALUES (:src, :kind, :sev, :title, :details)
    """), {"src": source, "kind": kind, "sev": severity, "title": title, "details": details})
    return True


def _auto_resolve(conn, text, source: str, kind: str):
    """Fecha incidentes open deste source+kind se a condição já não existe."""
    conn.execute(text("""
        UPDATE public.incidents
        SET status='resolved', resolved_at=NOW()
        WHERE source=:s AND kind=:k AND status='open'
    """), {"s": source, "k": kind})


def check_workers(conn, text):
    """Workers com last_seen > 5min → crit."""
    rows = conn.execute(text("""
        SELECT id, hostname, last_seen
        FROM public.workers
        WHERE last_seen < NOW() - INTERVAL '5 minutes'
          AND status != 'offline'
    """)).mappings().all()
    for w in rows:
        _ensure_incident(conn, text, "workers", f"worker_offline_{w['id']}", "crit",
                         f"Worker offline: {w['hostname']}",
                         f"Último heartbeat: {str(w['last_seen'])[:16]}")

    # Fechar incidentes de workers que voltaram online
    back = conn.execute(text("""
        SELECT id FROM public.workers
        WHERE last_seen >= NOW() - INTERVAL '5 minutes'
    """)).mappings().all()
    for w in back:
        _auto_resolve(conn, text, "workers", f"worker_offline_{w['id']}")


def check_tasks(conn, text):
    """Tarefas twin_tasks em pending há mais de 24h → warn."""
    rows = conn.execute(text("""
        SELECT t.id, t.title, t.created_at, e.name AS entity_name
        FROM public.twin_tasks t
        JOIN public.twin_cases c ON c.id = t.case_id
        JOIN public.twin_entities e ON e.id = c.entity_id
        WHERE t.status = 'pending'
          AND t.created_at < NOW() - INTERVAL '24 hours'
        LIMIT 10
    """)).mappings().all()
    if rows:
        _ensure_incident(conn, text, "tasks", "tasks_blocked", "warn",
                         f"{len(rows)} tarefas bloqueadas há mais de 24h",
                         "\n".join(f"#{r['id']} {r['title']} ({r['entity_name']})" for r in rows))
    else:
        _auto_resolve(conn, text, "tasks", "tasks_blocked")


def check_obligations(conn, text):
    """Obrigações fiscais vencidas → crit; < 5 dias → warn."""
    now = datetime.now(timezone.utc).date()
    overdue = conn.execute(text("""
        SELECT id, label, due_date FROM public.finance_obligations
        WHERE status = 'pending' AND due_date < CURRENT_DATE
        ORDER BY due_date
    """)).mappings().all()
    if overdue:
        titles = ", ".join(f"{r['label']} ({str(r['due_date'])[:10]})" for r in overdue)
        _ensure_incident(conn, text, "finance", "obligation_overdue", "crit",
                         f"{len(overdue)} obrigações fiscais vencidas",
                         titles)
    else:
        _auto_resolve(conn, text, "finance", "obligation_overdue")

    upcoming = conn.execute(text("""
        SELECT id, label, due_date FROM public.finance_obligations
        WHERE status = 'pending'
          AND due_date >= CURRENT_DATE
          AND due_date <= CURRENT_DATE + INTERVAL '5 days'
        ORDER BY due_date
    """)).mappings().all()
    if upcoming:
        titles = ", ".join(f"{r['label']} ({str(r['due_date'])[:10]})" for r in upcoming)
        _ensure_incident(conn, text, "finance", "obligation_soon", "warn",
                         f"{len(upcoming)} obrigações fiscais nos próximos 5 dias",
                         titles)
    else:
        _auto_resolve(conn, text, "finance", "obligation_soon")


def check_tenders(conn, text):
    """Tenders com deadline ≤ 3 dias → crit."""
    rows = conn.execute(text("""
        SELECT e.id, e.name, e.metadata->>'deadline' AS deadline
        FROM public.twin_entities e
        WHERE e.type = 'tender'
          AND e.metadata->>'estado' NOT IN ('fechado','rejeitado','expirado')
          AND e.metadata->>'deadline' IS NOT NULL
          AND e.metadata->>'deadline' != ''
          AND (e.metadata->>'deadline') ~ '^\d{4}-\d{2}-\d{2}'
          AND CAST(e.metadata->>'deadline' AS DATE) <= CURRENT_DATE + INTERVAL '3 days'
          AND CAST(e.metadata->>'deadline' AS DATE) >= CURRENT_DATE
        LIMIT 10
    """)).mappings().all()
    if rows:
        titles = ", ".join(f"{r['name']} (deadline {str(r['deadline'])[:10]})" for r in rows)
        _ensure_incident(conn, text, "tender", "tender_deadline", "crit",
                         f"{len(rows)} concursos com prazo nos próximos 3 dias",
                         titles)
    else:
        _auto_resolve(conn, text, "tender", "tender_deadline")


def check_toconline():
    """Toconline health check."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://127.0.0.1:3000/api/finance/toconline/health", timeout=5) as r:
            data = json.loads(r.read())
        return data.get("connected", False)
    except Exception:
        return None


def check_api(conn, text):
    """APIs externas falhadas → warn."""
    toc = check_toconline()
    if toc is False:
        _ensure_incident(conn, text, "infra", "toconline_disconnected", "warn",
                         "Toconline desligado — token expirado",
                         "Actualizar ~/.toc_token.json com novo token")
    elif toc is True:
        _auto_resolve(conn, text, "infra", "toconline_disconnected")
    # Se None (UI não responde) — não cria incidente (pode ser arranque)


def check_bank(conn, text):
    """Movimentos bancários sem match há mais de 72h → info."""
    rows = conn.execute(text("""
        SELECT COUNT(*) AS cnt FROM public.bank_transactions
        WHERE status = 'unmatched'
          AND imported_at < NOW() - INTERVAL '72 hours'
    """)).mappings().first()
    cnt = rows["cnt"] if rows else 0
    if cnt > 0:
        _ensure_incident(conn, text, "bank", "bank_unmatched", "info",
                         f"{cnt} movimentos bancários por reconciliar (> 72h)",
                         "Aceder a /finance → Reconciliação Bancária")
    else:
        _auto_resolve(conn, text, "bank", "bank_unmatched")


def run():
    engine, text = _conn()
    for fn in [check_workers, check_tasks, check_obligations, check_tenders, check_bank]:
        try:
            with engine.begin() as conn:
                fn(conn, text)
        except Exception as e:
            print(f"[incidents_tick] erro em {fn.__name__}: {e}", file=sys.stderr)
    try:
        with engine.begin() as conn:
            check_api(conn, text)
    except Exception as e:
        print(f"[incidents_tick] erro em check_api: {e}", file=sys.stderr)

    # Contar incidentes open para log
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT COUNT(*) AS n FROM public.incidents WHERE status='open'"
        )).mappings().first()
        n = row["n"] if row else 0
    print(f"[incidents_tick] done — {n} incidentes open")


if __name__ == "__main__":
    run()
