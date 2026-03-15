#!/usr/bin/env python3
# Remover bin/ do sys.path para evitar shadowing do stdlib (ex: bin/secrets.py)
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

"""
noc_query.py — queries NOC ao Postgres para o server.js Express.

Uso: python3 noc_query.py <comando> [args...]

Comandos:
  telemetry_history [n] [host]  — últimas N leituras de telemetria
  telemetry_live                — leitura mais recente por host
  workers                       — lista de workers (status, last_seen)
  worker_register <id> <host> <role>  — upsert worker
  worker_jobs [limit]           — jobs recentes (queued/running/done/failed)
  worker_jobs_lease <worker_id> — lease próximo job queued
  worker_jobs_report <job_id> <status> <result_json>  — report resultado
  events [n]                    — últimos N eventos
  backlog_recent [limit]        — tasks recentes do backlog
  syshealth                     — saúde: docker + timers + backlog count

Output: JSON para stdout; erros para stderr.
"""
import json
import os
import secrets as _secrets
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

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


# ── telemetry_history ─────────────────────────────────────────────────────────

def cmd_telemetry_history(args):
    n    = int(args[0]) if args else 120
    host = args[1] if len(args) > 1 else None
    engine, text = _conn()
    with engine.connect() as c:
        if host:
            q = text("""
                SELECT ts, hostname, cpu_pct, mem_used_mb, mem_total_mb,
                       disk_used_gb, disk_total_gb, load1, backlog_pending
                FROM public.telemetry
                WHERE hostname = :host
                ORDER BY ts DESC LIMIT :n
            """)
            rows = c.execute(q, {"host": host, "n": n}).mappings().all()
        else:
            q = text("""
                SELECT ts, hostname, cpu_pct, mem_used_mb, mem_total_mb,
                       disk_used_gb, disk_total_gb, load1, backlog_pending
                FROM public.telemetry
                ORDER BY ts DESC LIMIT :n
            """)
            rows = c.execute(q, {"n": n}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── telemetry_live ────────────────────────────────────────────────────────────

def cmd_telemetry_live(_args):
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT DISTINCT ON (hostname)
                ts, hostname, cpu_pct, mem_used_mb, mem_total_mb,
                disk_used_gb, disk_total_gb, load1, backlog_pending
            FROM public.telemetry
            ORDER BY hostname, ts DESC
        """)).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── workers ───────────────────────────────────────────────────────────────────

def cmd_workers(_args):
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, hostname, role, status,
                   last_seen,
                   EXTRACT(EPOCH FROM (NOW() - last_seen))::int AS age_secs
            FROM public.workers
            ORDER BY last_seen DESC
        """)).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── worker_register ───────────────────────────────────────────────────────────

def cmd_worker_register(args):
    if len(args) < 3:
        raise ValueError("usage: worker_register <id> <hostname> <role>")
    wid, hostname, role = args[0], args[1], args[2]
    engine, text = _conn()
    with engine.begin() as c:
        c.execute(text("""
            INSERT INTO public.workers (id, hostname, role, status, last_seen)
            VALUES (:id, :hostname, :role, 'online', NOW())
            ON CONFLICT (id) DO UPDATE
              SET hostname=EXCLUDED.hostname, role=EXCLUDED.role,
                  status='online', last_seen=NOW()
        """), {"id": wid, "hostname": hostname, "role": role})
    print(json.dumps({"ok": True, "id": wid}))


# ── worker_jobs ───────────────────────────────────────────────────────────────

def cmd_worker_jobs(args):
    limit = int(args[0]) if args else 30
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, ts_created, ts_assigned, ts_done, status,
                   target_worker_id, assigned_worker_id, kind,
                   payload, result
            FROM public.worker_jobs
            ORDER BY ts_created DESC LIMIT :n
        """), {"n": limit}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── worker_jobs_lease ─────────────────────────────────────────────────────────

def cmd_worker_jobs_lease(args):
    if not args:
        raise ValueError("usage: worker_jobs_lease <worker_id>")
    worker_id = args[0]
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            UPDATE public.worker_jobs
            SET status='running', assigned_worker_id=:wid, ts_assigned=NOW()
            WHERE id = (
                SELECT id FROM public.worker_jobs
                WHERE status='queued'
                  AND (target_worker_id IS NULL OR target_worker_id=:wid)
                ORDER BY ts_created ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            RETURNING id, kind, payload
        """), {"wid": worker_id}).mappings().first()
    if row:
        print(json.dumps({"ok": True, "job": _row(row)}))
    else:
        print(json.dumps({"ok": False, "job": None}))


# ── worker_jobs_report ────────────────────────────────────────────────────────

def cmd_worker_jobs_report(args):
    if len(args) < 3:
        raise ValueError("usage: worker_jobs_report <job_id> <status> <result_json>")
    job_id, status, result_raw = args[0], args[1], args[2]
    try:
        result = json.loads(result_raw)
    except Exception:
        result = {"raw": result_raw}
    engine, text = _conn()
    with engine.begin() as c:
        c.execute(text("""
            UPDATE public.worker_jobs
            SET status=:status, result=:result, ts_done=NOW()
            WHERE id=:id
        """), {"id": int(job_id), "status": status, "result": json.dumps(result)})
    print(json.dumps({"ok": True}))


# ── events ────────────────────────────────────────────────────────────────────

def cmd_events(args):
    n = int(args[0]) if args else 30
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, ts, level, source, kind, message
            FROM public.events
            ORDER BY ts DESC LIMIT :n
        """), {"n": n}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── backlog_recent ────────────────────────────────────────────────────────────

def cmd_backlog_recent(args):
    limit = int(args[0]) if args else 20
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, goal AS title, goal, status,
                   meta->>'priority'  AS priority,
                   meta->>'task_type' AS task_type,
                   (meta->>'attempts')::int AS attempts,
                   meta->>'last_error' AS last_error,
                   created_at, updated_at
            FROM public.jobs
            ORDER BY created_at DESC LIMIT :n
        """), {"n": limit}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── syshealth ─────────────────────────────────────────────────────────────────

def _run(cmd: str, timeout: int = 5) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def cmd_syshealth(_args):
    # Docker containers
    docker_raw = _run(
        "docker ps --format '{\"name\":\"{{.Names}}\",\"status\":\"{{.Status}}\",\"state\":\"{{.State}}\"}'"
    )
    containers = []
    for line in docker_raw.splitlines():
        line = line.strip()
        if line:
            try:
                containers.append(json.loads(line))
            except Exception:
                containers.append({"name": line, "state": "unknown"})

    # Systemd timers
    timers_raw = _run(
        "systemctl list-timers --no-pager --all --output=json 2>/dev/null "
        "| python3 -c \"import json,sys; ts=json.load(sys.stdin); "
        "print(json.dumps([{'unit':t.get('unit',''),'next':t.get('next',''),'last':t.get('last','')} "
        "for t in ts if 'aios' in t.get('unit','').lower()]))\" 2>/dev/null"
    )
    try:
        timers = json.loads(timers_raw) if timers_raw else []
    except Exception:
        timers = []

    # Backlog counts from PG
    counts = {"pending": 0, "running": 0, "done": 0, "failed": 0}
    try:
        engine, text = _conn()
        with engine.connect() as c:
            rows = c.execute(text(
                "SELECT status, count(*) as n FROM public.jobs GROUP BY status"
            )).mappings().all()
            for r in rows:
                counts[r["status"]] = int(r["n"])
    except Exception:
        pass

    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "containers": containers,
        "timers": timers,
        "backlog": counts,
    }
    print(json.dumps(out, ensure_ascii=False))


# ── worker_jobs_enqueue ───────────────────────────────────────────────────────

def cmd_worker_jobs_enqueue(args):
    """Enfileira job. Args: <kind> <payload_json> [target_worker_id|-]"""
    if len(args) < 2:
        raise ValueError("usage: worker_jobs_enqueue <kind> <payload_json> [target_worker_id]")
    kind    = args[0]
    payload = json.loads(args[1])
    target  = args[2] if len(args) > 2 and args[2] != "-" else None
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text(
            "INSERT INTO public.worker_jobs (ts_created, status, kind, payload, target_worker_id) "
            "VALUES (NOW(), 'queued', :kind, :payload, :target) RETURNING id"
        ), {"kind": kind, "payload": json.dumps(payload), "target": target})
        job_id = row.scalar()
    print(json.dumps({"ok": True, "job_id": job_id}))


# ── worker_token_check ────────────────────────────────────────────────────────

def cmd_worker_token_check(args):
    if not args:
        raise ValueError("usage: worker_token_check <worker_id>")
    wid = args[0]
    engine, text = _conn()
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT token FROM public.workers WHERE id=:wid LIMIT 1"
        ), {"wid": wid}).mappings().first()
    print(json.dumps({"token": row["token"] if row and row["token"] else None}))


# ── twin_cases ────────────────────────────────────────────────────────────────

def cmd_twin_cases(args):
    limit = int(args[0]) if args else 20
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT c.id, c.workflow_key, c.status, c.created_at, c.updated_at,
                   e.id AS entity_id, e.name AS entity_name, e.type AS entity_type,
                   e.metadata->>'kg' AS kg,
                   e.metadata->>'client' AS client,
                   e.metadata->>'estado' AS estado,
                   e.metadata->>'kg_cobre' AS kg_cobre,
                   e.metadata->>'kg_plastico' AS kg_plastico,
                   e.metadata->>'valor_fatura' AS valor_fatura
            FROM public.twin_cases c
            LEFT JOIN public.twin_entities e ON e.id = c.entity_id
            ORDER BY c.created_at DESC LIMIT :n
        """), {"n": limit}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── twin_cable_batch_create ───────────────────────────────────────────────────

def cmd_twin_cable_batch_create(args):
    """Cria entity batch + case cable_batch + tasks iniciais.
    Args: <kg> <client> [tenant_id]
    """
    if len(args) < 2:
        raise ValueError("usage: twin_cable_batch_create <kg> <client>")
    kg     = float(args[0])
    client = args[1]
    tenant = args[2] if len(args) > 2 else "jdl"
    engine, text = _conn()
    with engine.begin() as c:
        # 1. entidade batch
        client_token = _secrets.token_urlsafe(16)
        entity_id = c.execute(text("""
            INSERT INTO public.twin_entities (tenant_id, type, name, status, metadata)
            VALUES (:tenant, 'batch', :name, 'active', :meta)
            RETURNING id
        """), {
            "tenant": tenant,
            "name": f"Lote {client} {kg}kg",
            "meta": json.dumps({"kg": kg, "client": client, "estado": "agendado",
                                "client_token": client_token}),
        }).scalar()

        # 2. caso cable_batch
        case_id = c.execute(text("""
            INSERT INTO public.twin_cases (tenant_id, workflow_key, entity_id, status, data)
            VALUES (:tenant, 'cable_batch', :eid, 'open', :data)
            RETURNING id
        """), {
            "tenant": tenant,
            "eid": entity_id,
            "data": json.dumps({"kg": kg, "client": client, "estado": "agendado"}),
        }).scalar()

        # 3. tasks iniciais do workflow
        tasks = [
            ("Check-in do lote na fábrica",    "human"),
            ("Iniciar processamento",           "human"),
            ("Registar resultado (cobre/plástico)", "human"),
            ("Fechar lote e faturar",           "human"),
        ]
        for title, ttype in tasks:
            c.execute(text("""
                INSERT INTO public.twin_tasks (case_id, title, type, status)
                VALUES (:cid, :title, :type, 'pending')
            """), {"cid": case_id, "title": title, "type": ttype})

        # 4. evento
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, entity_id, message, data)
            VALUES (NOW(), 'info', 'twin', 'batch_created', :eid, :msg, :data)
        """), {
            "eid": entity_id,
            "msg": f"Lote criado: {client} {kg}kg",
            "data": json.dumps({"entity_id": entity_id, "case_id": case_id, "kg": kg, "client": client}),
        })

    print(json.dumps({"ok": True, "entity_id": entity_id, "case_id": case_id,
                      "kg": kg, "client": client, "estado": "agendado",
                      "client_token": client_token}))


# ── twin_batch_get ────────────────────────────────────────────────────────────

BATCH_STATES = ["agendado", "chegou", "em_processamento", "separacao",
                "concluido", "pronto_levantar", "faturado", "fechado"]

def cmd_twin_batch_get(args):
    if not args:
        raise ValueError("usage: twin_batch_get <entity_id>")
    eid = int(args[0])
    engine, text = _conn()
    with engine.connect() as c:
        entity = c.execute(text(
            "SELECT id, name, status, metadata FROM public.twin_entities WHERE id=:id"
        ), {"id": eid}).mappings().first()
        if not entity:
            print(json.dumps({"error": f"lote #{eid} não encontrado"}))
            return
        case = c.execute(text(
            "SELECT id, status, data FROM public.twin_cases WHERE entity_id=:eid ORDER BY id DESC LIMIT 1"
        ), {"eid": eid}).mappings().first()
        tasks = c.execute(text(
            "SELECT id, title, type, status FROM public.twin_tasks WHERE case_id=:cid ORDER BY id ASC"
        ), {"cid": case["id"]}).mappings().all() if case else []

    meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
    estado = meta.get("estado", "?")
    idx    = BATCH_STATES.index(estado) if estado in BATCH_STATES else -1
    proximo = BATCH_STATES[idx + 1] if idx >= 0 and idx < len(BATCH_STATES) - 1 else None

    print(json.dumps({
        "entity_id": entity["id"],
        "name": entity["name"],
        "kg": meta.get("kg"),
        "client": meta.get("client"),
        "estado": estado,
        "proximo_estado": proximo,
        "case_id": case["id"] if case else None,
        "case_status": case["status"] if case else None,
        "tasks": [_row(t) for t in tasks],
    }, ensure_ascii=False))


# ── twin_batch_advance ────────────────────────────────────────────────────────

def cmd_twin_batch_advance(args):
    """Avança estado do lote para o próximo. Args: <entity_id> [nota]"""
    if not args:
        raise ValueError("usage: twin_batch_advance <entity_id> [nota]")
    eid  = int(args[0])
    nota = args[1] if len(args) > 1 else ""
    engine, text = _conn()
    with engine.begin() as c:
        entity = c.execute(text(
            "SELECT id, metadata FROM public.twin_entities WHERE id=:id FOR UPDATE"
        ), {"id": eid}).mappings().first()
        if not entity:
            print(json.dumps({"error": f"lote #{eid} não encontrado"}))
            return
        meta   = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
        estado = meta.get("estado", BATCH_STATES[0])
        idx    = BATCH_STATES.index(estado) if estado in BATCH_STATES else -1
        if idx < 0 or idx >= len(BATCH_STATES) - 1:
            print(json.dumps({"error": f"lote já em estado final: {estado}"}))
            return
        novo_estado = BATCH_STATES[idx + 1]
        meta["estado"] = novo_estado

        # actualiza entidade
        c.execute(text(
            "UPDATE public.twin_entities SET metadata=:meta, updated_at=NOW() WHERE id=:id"
        ), {"meta": json.dumps(meta), "id": eid})

        # actualiza case data
        case = c.execute(text(
            "SELECT id FROM public.twin_cases WHERE entity_id=:eid ORDER BY id DESC LIMIT 1"
        ), {"eid": eid}).mappings().first()
        if case:
            case_status_sql = ", status='closed'" if novo_estado == "fechado" else ""
            c.execute(text(
                f"UPDATE public.twin_cases SET data=jsonb_set(data, '{{estado}}', CAST(:est AS jsonb)){case_status_sql}, updated_at=NOW() WHERE id=:cid"
            ), {"est": json.dumps(novo_estado), "cid": case["id"]})
            # marca primeira task pending como done
            first_pending = c.execute(text(
                "SELECT id FROM public.twin_tasks WHERE case_id=:cid AND status='pending' ORDER BY id ASC LIMIT 1"
            ), {"cid": case["id"]}).mappings().first()
            if first_pending:
                c.execute(text(
                    "UPDATE public.twin_tasks SET status='done', updated_at=NOW() WHERE id=:tid"
                ), {"tid": first_pending["id"]})

        # evento
        msg = f"Lote #{eid} {estado} → {novo_estado}"
        if nota:
            msg += f" ({nota})"
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(), 'info', 'twin', 'batch_advanced', :eid, :msg, :data)"
        ), {"eid": eid, "msg": msg, "data": json.dumps({"entity_id": eid, "de": estado, "para": novo_estado, "nota": nota})})

    print(json.dumps({"ok": True, "entity_id": eid, "de": estado, "para": novo_estado}))


# ── twin_batch_faturar ────────────────────────────────────────────────────────

def cmd_twin_batch_faturar(args):
    """Cria approval de faturação. Args: <entity_id> <preco_kg>"""
    if len(args) < 2:
        raise ValueError("usage: twin_batch_faturar <entity_id> <preco_kg>")
    eid      = int(args[0])
    preco_kg = float(args[1])
    engine, text = _conn()
    with engine.begin() as c:
        entity = c.execute(text(
            "SELECT id, metadata FROM public.twin_entities WHERE id=:id"
        ), {"id": eid}).mappings().first()
        if not entity:
            print(json.dumps({"error": f"lote #{eid} não encontrado"}))
            return
        meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
        kg_cobre = float(meta.get("kg_cobre") or 0)
        if not kg_cobre:
            print(json.dumps({"error": "resultado não registado — use /lote resultado primeiro"}))
            return
        valor = round(kg_cobre * preco_kg, 2)
        meta["preco_kg"]     = preco_kg
        meta["valor_fatura"] = valor
        c.execute(text(
            "UPDATE public.twin_entities SET metadata=:meta, updated_at=NOW() WHERE id=:id"
        ), {"meta": json.dumps(meta), "id": eid})
        case = c.execute(text(
            "SELECT id FROM public.twin_cases WHERE entity_id=:eid ORDER BY id DESC LIMIT 1"
        ), {"eid": eid}).mappings().first()
        summary = f"Lote #{eid} — {meta.get('client','?')} — {kg_cobre}kg Cu × €{preco_kg}/kg = €{valor}"
        approval_id = c.execute(text("""
            INSERT INTO public.twin_approvals
              (case_id, action, status, requested_by, summary, context)
            VALUES (:cid, 'faturar_lote', 'pending', 'system', :summary, :ctx)
            RETURNING id
        """), {
            "cid": case["id"] if case else None,
            "summary": summary,
            "ctx": json.dumps({"entity_id": eid, "kg_cobre": kg_cobre, "preco_kg": preco_kg, "valor": valor}),
        }).scalar()
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(), 'info', 'twin', 'fatura_pendente', :eid, :msg, :data)"
        ), {
            "eid": eid,
            "msg": f"Faturação pendente: Lote #{eid} €{valor}",
            "data": json.dumps({"entity_id": eid, "valor": valor, "approval_id": approval_id}),
        })
    print(json.dumps({"ok": True, "entity_id": eid, "valor": valor,
                      "kg_cobre": kg_cobre, "preco_kg": preco_kg,
                      "approval_id": approval_id, "summary": summary}))


# ── twin_batch_faturar_ok ─────────────────────────────────────────────────────

def cmd_twin_batch_faturar_ok(args):
    """Marca lote como faturado (chamado ao aprovar). Args: <entity_id>"""
    if not args:
        raise ValueError("usage: twin_batch_faturar_ok <entity_id>")
    eid = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        entity = c.execute(text(
            "SELECT id, metadata FROM public.twin_entities WHERE id=:id FOR UPDATE"
        ), {"id": eid}).mappings().first()
        if not entity:
            print(json.dumps({"error": f"lote #{eid} não encontrado"}))
            return
        meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
        meta["estado"] = "faturado"
        c.execute(text(
            "UPDATE public.twin_entities SET metadata=:meta, updated_at=NOW() WHERE id=:id"
        ), {"meta": json.dumps(meta), "id": eid})
        case = c.execute(text(
            "SELECT id FROM public.twin_cases WHERE entity_id=:eid ORDER BY id DESC LIMIT 1"
        ), {"eid": eid}).mappings().first()
        if case:
            c.execute(text(
                "UPDATE public.twin_cases SET data=jsonb_set(data, '{estado}', '\"faturado\"'::jsonb), "
                "updated_at=NOW() WHERE id=:cid"
            ), {"cid": case["id"]})
            c.execute(text(
                "UPDATE public.twin_tasks SET status='done', updated_at=NOW() "
                "WHERE case_id=:cid AND status='pending'"
            ), {"cid": case["id"]})
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(), 'info', 'twin', 'batch_faturado', :eid, :msg, :data)"
        ), {
            "eid": eid,
            "msg": f"Lote #{eid} faturado — €{meta.get('valor_fatura','?')}",
            "data": json.dumps({"entity_id": eid, "valor": meta.get("valor_fatura")}),
        })
        # Auto-criar invoice se ainda não existir
        existing_inv = c.execute(text(
            "SELECT id FROM public.twin_invoices WHERE entity_id=:eid LIMIT 1"
        ), {"eid": eid}).fetchone()
        invoice_number = None
        if not existing_inv:
            valor = float(meta.get("valor_fatura") or 0)
            yr    = datetime.now().year
            cnt   = c.execute(text(
                "SELECT COUNT(*) FROM public.twin_invoices WHERE number LIKE :pat"
            ), {"pat": f"AIOS-{yr}-%"}).scalar()
            invoice_number = f"AIOS-{yr}-{cnt+1:04d}"
            due_date = (datetime.now() + timedelta(days=30)).date()
            case_id_inv = case["id"] if case else None
            c.execute(text("""
                INSERT INTO public.twin_invoices
                  (entity_id, case_id, number, status, amount, client, due_date)
                VALUES (:eid, :cid, :num, 'issued', :amt, :cli, :due)
            """), {"eid": eid, "cid": case_id_inv,
                   "num": invoice_number, "amt": valor,
                   "cli": meta.get("client"), "due": due_date})
            c.execute(text(
                "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
                "VALUES (NOW(),'info','twin','invoice_created',:eid,:msg,:data)"
            ), {"eid": eid, "msg": f"Fatura {invoice_number} emitida — €{valor:.2f}",
                "data": json.dumps({"invoice_number": invoice_number, "amount": valor, "entity_id": eid})})
    print(json.dumps({"ok": True, "entity_id": eid, "estado": "faturado",
                      "valor": meta.get("valor_fatura"), "invoice_number": invoice_number}))


# ── twin_factory_stats ────────────────────────────────────────────────────────

def cmd_twin_factory_stats(_args):
    engine, text = _conn()
    with engine.connect() as c:
        by_estado = c.execute(text("""
            SELECT e.metadata->>'estado' AS estado, COUNT(*) AS n
            FROM public.twin_cases c
            JOIN public.twin_entities e ON e.id = c.entity_id
            WHERE c.workflow_key = 'cable_batch'
            GROUP BY e.metadata->>'estado'
            ORDER BY MIN(c.created_at)
        """)).mappings().all()
        hoje = c.execute(text("""
            SELECT
                COUNT(*) AS lotes_hoje,
                COALESCE(SUM(CAST(NULLIF(e.metadata->>'kg','') AS FLOAT)), 0) AS kg_entrada,
                COALESCE(SUM(CAST(NULLIF(e.metadata->>'kg_cobre','') AS FLOAT)), 0) AS kg_cobre,
                COALESCE(SUM(CAST(NULLIF(e.metadata->>'kg_plastico','') AS FLOAT)), 0) AS kg_plastico,
                COALESCE(SUM(CAST(NULLIF(e.metadata->>'valor_fatura','') AS FLOAT)), 0) AS receita
            FROM public.twin_cases c
            JOIN public.twin_entities e ON e.id = c.entity_id
            WHERE c.workflow_key = 'cable_batch'
              AND c.created_at >= CURRENT_DATE
        """)).mappings().first()
        total = c.execute(text("""
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE e.metadata->>'estado' != 'fechado') AS ativos
            FROM public.twin_cases c
            JOIN public.twin_entities e ON e.id = c.entity_id
            WHERE c.workflow_key = 'cable_batch'
        """)).mappings().first()
    print(json.dumps({
        "by_estado": [_row(r) for r in by_estado],
        "hoje":  _row(hoje) if hoje else {},
        "total": _row(total) if total else {},
    }, ensure_ascii=False))


# ── twin_batch_by_token ───────────────────────────────────────────────────────

def cmd_twin_batch_by_token(args):
    """Lote por client_token (portal cliente público). Args: <token>"""
    if not args:
        raise ValueError("usage: twin_batch_by_token <token>")
    token = args[0]
    engine, text = _conn()
    with engine.connect() as c:
        entity = c.execute(text(
            "SELECT id, name, status, metadata, created_at FROM public.twin_entities "
            "WHERE metadata->>'client_token' = :tok AND type='batch' LIMIT 1"
        ), {"tok": token}).mappings().first()
        if not entity:
            print(json.dumps({"error": "lote não encontrado"}))
            return
        eid  = entity["id"]
        meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
        case = c.execute(text(
            "SELECT id, status FROM public.twin_cases WHERE entity_id=:eid ORDER BY id DESC LIMIT 1"
        ), {"eid": eid}).mappings().first()
        tasks = []
        if case:
            tasks = c.execute(text(
                "SELECT title, status, updated_at FROM public.twin_tasks WHERE case_id=:cid ORDER BY id ASC"
            ), {"cid": case["id"]}).mappings().all()
        events = c.execute(text(
            "SELECT ts, kind, message FROM public.events "
            "WHERE entity_id=:eid AND source='twin' ORDER BY ts ASC"
        ), {"eid": eid}).mappings().all()
        # Approvals pendentes para este cliente
        approvals = c.execute(text(
            "SELECT id, action, status, context, requested_at FROM public.twin_approvals "
            "WHERE context->>'entity_id' = :eid AND status = 'pending' "
            "ORDER BY requested_at DESC"
        ), {"eid": str(eid)}).mappings().all()
        # Documentos disponíveis
        documents = c.execute(text(
            "SELECT id, template_key, status, created_at FROM public.twin_documents "
            "WHERE entity_id = :eid AND status = 'ready' ORDER BY created_at DESC"
        ), {"eid": eid}).mappings().all()
    estado  = meta.get("estado", "?")
    idx     = BATCH_STATES.index(estado) if estado in BATCH_STATES else 0
    progresso = round(idx / (len(BATCH_STATES) - 1) * 100) if len(BATCH_STATES) > 1 else 0
    # valor_fatura só é visível ao cliente quando fatura já foi aprovada/fechada
    estados_com_fatura = {"faturado", "fechado"}
    valor_fatura = meta.get("valor_fatura") if estado in estados_com_fatura else None
    print(json.dumps({
        "entity_id": eid,
        "name": entity["name"],
        "kg": meta.get("kg"),
        "client": meta.get("client"),
        "estado": estado,
        "progresso_pct": progresso,
        "kg_cobre": meta.get("kg_cobre"),
        "kg_plastico": meta.get("kg_plastico"),
        "valor_fatura": valor_fatura,
        "created_at": entity["created_at"].isoformat() if entity["created_at"] else None,
        "tasks": [_row(t) for t in tasks],
        "events": [{"ts": str(e["ts"])[:16].replace("T", " "), "msg": e["message"]} for e in events],
        "approvals": [{"id": a["id"], "action": a["action"], "status": a["status"],
                       "context": a["context"] if isinstance(a["context"], dict) else json.loads(a["context"] or "{}"),
                       "requested_at": str(a["requested_at"])[:16]} for a in approvals],
        "documents": [{"id": d["id"], "type": d["template_key"], "status": d["status"]} for d in documents],
    }, ensure_ascii=False))


# ── client_approve ────────────────────────────────────────────────────────────

def cmd_client_approve(args):
    """Aprovação pelo cliente via portal. Args: <token> <approval_id> <action: approved|rejected>"""
    if len(args) < 3:
        raise ValueError("usage: client_approve <token> <approval_id> <approved|rejected>")
    token       = args[0]
    approval_id = int(args[1])
    action      = args[2]
    if action not in ("approved", "rejected"):
        print(json.dumps({"ok": False, "error": "action deve ser 'approved' ou 'rejected'"}))
        return
    engine, text = _conn()
    with engine.begin() as c:
        # Validar token → entity
        entity = c.execute(text(
            "SELECT id FROM public.twin_entities "
            "WHERE metadata->>'client_token' = :tok AND type='batch' LIMIT 1"
        ), {"tok": token}).mappings().first()
        if not entity:
            print(json.dumps({"ok": False, "error": "token inválido"}))
            return
        eid = entity["id"]
        # Validar approval pertence a esta entity
        appr = c.execute(text(
            "SELECT id, action, status, context FROM public.twin_approvals "
            "WHERE id=:aid AND context->>'entity_id' = :eid FOR UPDATE"
        ), {"aid": approval_id, "eid": str(eid)}).mappings().first()
        if not appr:
            print(json.dumps({"ok": False, "error": "aprovação não encontrada para este lote"}))
            return
        if appr["status"] != "pending":
            print(json.dumps({"ok": False, "error": f"aprovação já foi decidida: {appr['status']}"}))
            return
        # Actualizar approval
        c.execute(text(
            "UPDATE public.twin_approvals SET status=:s, decided_at=NOW(), approved_by='client' "
            "WHERE id=:aid"
        ), {"s": action, "aid": approval_id})
        # Se faturar_lote aprovado → executar fatura_ok inline
        next_estado = None
        if action == "approved" and appr["action"] == "faturar_lote":
            ctx = appr["context"] if isinstance(appr["context"], dict) else json.loads(appr["context"] or "{}")
            valor = ctx.get("valor", 0)
            # Actualizar entity metadata
            entity_full = c.execute(text(
                "SELECT metadata FROM public.twin_entities WHERE id=:eid FOR UPDATE"
            ), {"eid": eid}).mappings().first()
            meta = entity_full["metadata"] if isinstance(entity_full["metadata"], dict) else json.loads(entity_full["metadata"] or "{}")
            meta["estado"]       = "faturado"
            meta["valor_fatura"] = valor
            c.execute(text(
                "UPDATE public.twin_entities SET metadata=:m, updated_at=NOW() WHERE id=:eid"
            ), {"m": json.dumps(meta), "eid": eid})
            # Marcar tasks pendentes como done
            c.execute(text(
                "UPDATE public.twin_tasks SET status='done', updated_at=NOW() "
                "WHERE case_id IN (SELECT id FROM public.twin_cases WHERE entity_id=:eid) "
                "AND status='pending'"
            ), {"eid": eid})
            # Evento
            c.execute(text(
                "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
                "VALUES (NOW(),'info','twin','fatura_aprovada_cliente',:eid,:msg,:data)"
            ), {
                "eid":  eid,
                "msg":  f"Faturação aprovada pelo cliente via portal (lote #{eid})",
                "data": json.dumps({"entity_id": eid, "valor": valor, "approval_id": approval_id}),
            })
            next_estado = "faturado"
        elif action == "rejected" and appr["action"] == "faturar_lote":
            c.execute(text(
                "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
                "VALUES (NOW(),'warn','twin','fatura_rejeitada_cliente',:eid,:msg,:data)"
            ), {
                "eid":  eid,
                "msg":  f"Faturação rejeitada pelo cliente via portal (lote #{eid})",
                "data": json.dumps({"entity_id": eid, "approval_id": approval_id}),
            })
    print(json.dumps({
        "ok": True,
        "approval_id": approval_id,
        "action":      action,
        "entity_id":   eid,
        "next_estado": next_estado,
    }))


# ── twin_batch_resultado ──────────────────────────────────────────────────────

def cmd_twin_batch_resultado(args):
    """Regista resultado (kg_cobre, kg_plastico) no lote.
    Args: <entity_id> <kg_cobre> <kg_plastico>
    """
    if len(args) < 3:
        raise ValueError("usage: twin_batch_resultado <entity_id> <kg_cobre> <kg_plastico>")
    eid       = int(args[0])
    kg_cobre  = float(args[1])
    kg_plastico = float(args[2])
    engine, text = _conn()
    with engine.begin() as c:
        entity = c.execute(text(
            "SELECT id, metadata FROM public.twin_entities WHERE id=:id FOR UPDATE"
        ), {"id": eid}).mappings().first()
        if not entity:
            print(json.dumps({"error": f"lote #{eid} não encontrado"}))
            return
        meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
        meta["kg_cobre"]    = kg_cobre
        meta["kg_plastico"] = kg_plastico
        meta["kg_residuo"]  = round(float(meta.get("kg", 0)) - kg_cobre - kg_plastico, 2)
        c.execute(text(
            "UPDATE public.twin_entities SET metadata=:meta, updated_at=NOW() WHERE id=:id"
        ), {"meta": json.dumps(meta), "id": eid})
        # evento
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(), 'info', 'twin', 'batch_resultado', :eid, :msg, :data)"
        ), {
            "eid": eid,
            "msg": f"Lote #{eid} resultado: {kg_cobre}kg cobre, {kg_plastico}kg plástico",
            "data": json.dumps({"entity_id": eid, "kg_cobre": kg_cobre, "kg_plastico": kg_plastico, "kg_residuo": meta["kg_residuo"]}),
        })
    print(json.dumps({"ok": True, "entity_id": eid, "kg_cobre": kg_cobre,
                      "kg_plastico": kg_plastico, "kg_residuo": meta["kg_residuo"]}))


# ── worker tasks ──────────────────────────────────────────────────────────────

def cmd_worker_tasks(args):
    """Lista tarefas pending/in_progress. Args: <username|all> [limit=30]"""
    user  = args[0] if args else "all"
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT t.id, t.title, t.type, t.status, t.assignee,
                   t.due_at, t.payload, t.created_at,
                   e.id AS entity_id, e.name AS entity_name,
                   e.metadata->>'kind' AS kind
            FROM public.twin_tasks t
            JOIN public.twin_cases c ON c.id = t.case_id
            JOIN public.twin_entities e ON e.id = c.entity_id
            WHERE t.status IN ('pending','in_progress')
              AND (:user = 'all' OR t.assignee IS NULL OR t.assignee = :user)
            ORDER BY
              CASE WHEN t.assignee = :user THEN 0 ELSE 1 END,
              CASE WHEN t.status = 'in_progress' THEN 0 ELSE 1 END,
              t.created_at ASC
            LIMIT :limit
        """), {"user": user, "limit": limit}).mappings().all()
    out = []
    for r in rows:
        pl = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        out.append({
            "id":          r["id"],
            "title":       r["title"],
            "type":        r["type"],
            "status":      r["status"],
            "assignee":    r["assignee"],
            "due_at":      r["due_at"].isoformat() if r["due_at"] else None,
            "started_at":  pl.get("started_at"),
            "entity_id":   r["entity_id"],
            "entity_name": r["entity_name"],
            "kind":        r["kind"],
            "created_at":  r["created_at"].isoformat() if r["created_at"] else "",
        })
    print(json.dumps(out, ensure_ascii=False))


def cmd_worker_tasks_history(args):
    """Tarefas done do worker. Args: <username> [limit=20]"""
    user  = args[0] if args else "all"
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT t.id, t.title, t.type, t.status, t.assignee,
                   t.payload, t.updated_at,
                   e.name AS entity_name
            FROM public.twin_tasks t
            JOIN public.twin_cases c ON c.id = t.case_id
            JOIN public.twin_entities e ON e.id = c.entity_id
            WHERE t.status = 'done'
              AND (:user = 'all' OR t.assignee = :user OR t.payload->>'done_by' = :user)
            ORDER BY t.updated_at DESC
            LIMIT :limit
        """), {"user": user, "limit": limit}).mappings().all()
    out = []
    for r in rows:
        pl = r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}")
        out.append({
            "id":           r["id"],
            "title":        r["title"],
            "status":       r["status"],
            "assignee":     r["assignee"] or pl.get("done_by"),
            "result":       pl.get("result", ""),
            "completed_at": pl.get("completed_at"),
            "entity_name":  r["entity_name"],
            "updated_at":   r["updated_at"].isoformat() if r["updated_at"] else "",
        })
    print(json.dumps(out, ensure_ascii=False))


def cmd_worker_task_start(args):
    """Inicia tarefa. Args: <task_id> <username>"""
    if len(args) < 2:
        raise ValueError("usage: worker_task_start <task_id> <username>")
    tid  = int(args[0])
    user = args[1]
    engine, text = _conn()
    with engine.begin() as c:
        task = c.execute(text(
            "SELECT t.id, t.status, c.entity_id FROM public.twin_tasks t "
            "JOIN public.twin_cases c ON c.id = t.case_id WHERE t.id = :id FOR UPDATE"
        ), {"id": tid}).mappings().first()
        if not task:
            print(json.dumps({"ok": False, "error": f"tarefa #{tid} não encontrada"}))
            return
        if task["status"] != "pending":
            print(json.dumps({"ok": False, "error": f"tarefa está em estado '{task['status']}', não 'pending'"}))
            return
        c.execute(text("""
            UPDATE public.twin_tasks
            SET status='in_progress', assignee=:user,
                payload=jsonb_set(payload, '{started_at}', to_jsonb(NOW()::text)),
                updated_at=NOW()
            WHERE id=:id
        """), {"id": tid, "user": user})
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(),'info','twin','worker_task_started',:eid,:msg,:data)"
        ), {
            "eid":  task["entity_id"],
            "msg":  f"Tarefa #{tid} iniciada por {user}",
            "data": json.dumps({"task_id": tid, "assignee": user}),
        })
    print(json.dumps({"ok": True, "task_id": tid, "assignee": user, "status": "in_progress"}))


def cmd_worker_task_done(args):
    """Conclui tarefa. Args: <task_id> <username> [note]"""
    if len(args) < 2:
        raise ValueError("usage: worker_task_done <task_id> <username> [note]")
    tid  = int(args[0])
    user = args[1]
    note = args[2] if len(args) > 2 else ""
    engine, text = _conn()
    with engine.begin() as c:
        task = c.execute(text(
            "SELECT t.id, t.status, t.assignee, c.entity_id FROM public.twin_tasks t "
            "JOIN public.twin_cases c ON c.id = t.case_id WHERE t.id = :id FOR UPDATE"
        ), {"id": tid}).mappings().first()
        if not task:
            print(json.dumps({"ok": False, "error": f"tarefa #{tid} não encontrada"}))
            return
        if task["status"] != "in_progress":
            print(json.dumps({"ok": False, "error": f"tarefa está em estado '{task['status']}', não 'in_progress'"}))
            return
        c.execute(text("""
            UPDATE public.twin_tasks
            SET status='done',
                payload=payload || jsonb_build_object(
                    'completed_at', NOW()::text,
                    'result', :note,
                    'done_by', :user
                ),
                updated_at=NOW()
            WHERE id=:id
        """), {"id": tid, "user": user, "note": note})
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(),'info','twin','worker_task_done',:eid,:msg,:data)"
        ), {
            "eid":  task["entity_id"],
            "msg":  f"Tarefa #{tid} concluída por {user}",
            "data": json.dumps({"task_id": tid, "done_by": user, "result": note}),
        })
    print(json.dumps({"ok": True, "task_id": tid, "done_by": user, "status": "done"}))


def cmd_worker_task_skip(args):
    """Salta tarefa. Args: <task_id> <username>"""
    if len(args) < 2:
        raise ValueError("usage: worker_task_skip <task_id> <username>")
    tid  = int(args[0])
    user = args[1]
    engine, text = _conn()
    with engine.begin() as c:
        task = c.execute(text(
            "SELECT t.id, t.status, c.entity_id FROM public.twin_tasks t "
            "JOIN public.twin_cases c ON c.id = t.case_id WHERE t.id = :id FOR UPDATE"
        ), {"id": tid}).mappings().first()
        if not task:
            print(json.dumps({"ok": False, "error": f"tarefa #{tid} não encontrada"}))
            return
        c.execute(text("""
            UPDATE public.twin_tasks
            SET status='skipped', assignee=:user, updated_at=NOW()
            WHERE id=:id
        """), {"id": tid, "user": user})
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(),'info','twin','worker_task_skipped',:eid,:msg,:data)"
        ), {
            "eid":  task["entity_id"],
            "msg":  f"Tarefa #{tid} saltada por {user}",
            "data": json.dumps({"task_id": tid, "skipped_by": user}),
        })
    print(json.dumps({"ok": True, "task_id": tid, "skipped_by": user, "status": "skipped"}))


# ── twin_tenders ──────────────────────────────────────────────────────────────

def cmd_twin_tenders(args):
    """Lista tenders. Args: [limit] [--source ted|base|dr] [--pin-sources]
    --pin-sources: garante ≥1 item por source no topo, resto por score (para NOC).
                   Sem flag: ranking puro por score (para /tenders).
    """
    limit       = 30
    source      = None
    pin_sources = False
    i = 0
    while i < len(args):
        if args[i] == "--source" and i + 1 < len(args):
            source = args[i + 1]; i += 2
        elif args[i] == "--pin-sources":
            pin_sources = True; i += 1
        elif args[i].isdigit():
            limit = int(args[i]); i += 1
        else:
            i += 1

    engine, text = _conn()
    src_filter = "AND e.metadata->>'source' = :src" if source else ""
    # With pin_sources fetch a pool large enough to find the top per source
    fetch_n = max(limit * 5, 200) if (pin_sources and not source) else limit
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT e.id, e.name, e.metadata, e.created_at,
                   tc.id AS case_id, tc.status AS case_status
            FROM public.twin_entities e
            LEFT JOIN public.twin_cases tc ON tc.entity_id = e.id AND tc.workflow_key = 'tender_intake'
            WHERE e.type = 'tender'
            {src_filter}
            ORDER BY (e.metadata->>'score')::int DESC NULLS LAST, e.created_at DESC
            LIMIT :n
        """), {"n": fetch_n, "src": source}).mappings().all()

    def _fmt(r):
        meta = r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"] or "{}")
        return {
            "entity_id":   r["id"],
            "title":       meta.get("title", r["name"]),
            "pub_num":     meta.get("pub_num", meta.get("external_id", "")),
            "score":       meta.get("score", 0),
            "estado":      meta.get("estado", "novo"),
            "deadline":    meta.get("deadline", ""),
            "nature":      meta.get("nature", ""),
            "grupo":       meta.get("grupo", ""),
            "pdf_url":     meta.get("pdf_url", ""),
            "source":      meta.get("source", "ted"),
            "entity_name": meta.get("entity_name", ""),
            "base_value":  meta.get("base_value"),
            "case_id":     r["case_id"],
            "case_status": r["case_status"],
            "created_at":  r["created_at"].isoformat() if r["created_at"] else "",
        }

    if pin_sources:
        # 1. Pick top-scored item per source (guaranteed, shown first, sorted by score desc)
        # 2. Fill remaining slots with the next best global items (no duplicates)
        seen_ids: set = set()
        seen_sources: set = set()
        guaranteed, rest = [], []
        for r in rows:
            meta = r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"] or "{}")
            src = meta.get("source", "ted")
            if src not in seen_sources:
                t = _fmt(r)
                guaranteed.append(t)
                seen_sources.add(src)
                seen_ids.add(r["id"])
            else:
                rest.append((r["id"], r))

        guaranteed.sort(key=lambda t: t["score"] or 0, reverse=True)
        fill = [_fmt(r) for rid, r in rest if rid not in seen_ids]
        out = guaranteed + fill[:max(0, limit - len(guaranteed))]
    else:
        # Pure score ranking — no pinning
        out = [_fmt(r) for r in rows]

    print(json.dumps(out[:limit], ensure_ascii=False))


def cmd_twin_tender_update(args):
    """Atualiza estado de um tender. Args: <entity_id> <estado>
    Estados: novo | a_analisar | candidatar | ignorar | submetido | ganho | perdido
    """
    if len(args) < 2:
        raise ValueError("usage: twin_tender_update <entity_id> <estado>")
    eid    = int(args[0])
    estado = args[1]
    VALID  = {"novo", "a_analisar", "candidatar", "ignorar", "submetido", "ganho", "perdido"}
    if estado not in VALID:
        raise ValueError(f"estado inválido: {estado}. Válidos: {', '.join(sorted(VALID))}")
    engine, text = _conn()
    with engine.begin() as c:
        entity = c.execute(text(
            "SELECT id, metadata FROM public.twin_entities WHERE id=:id AND type='tender' FOR UPDATE"
        ), {"id": eid}).mappings().first()
        if not entity:
            print(json.dumps({"error": f"tender #{eid} não encontrado"}))
            return
        meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
        meta["estado"] = estado
        c.execute(text(
            "UPDATE public.twin_entities SET metadata=:meta, updated_at=NOW() WHERE id=:id"
        ), {"meta": json.dumps(meta), "id": eid})
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(), 'info', 'twin', 'tender_updated', :eid, :msg, :data)"
        ), {
            "eid": eid,
            "msg": f"Tender #{eid} estado → {estado}",
            "data": json.dumps({"entity_id": eid, "estado": estado}),
        })
    print(json.dumps({"ok": True, "entity_id": eid, "estado": estado}))


# ── finance ───────────────────────────────────────────────────────────────────

def cmd_finance_invoice_create(args):
    """Cria invoice manualmente para entity. Args: <entity_id>"""
    if not args:
        raise ValueError("usage: finance_invoice_create <entity_id>")
    eid = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        entity = c.execute(text(
            "SELECT id, metadata FROM public.twin_entities WHERE id=:id"
        ), {"id": eid}).mappings().first()
        if not entity:
            print(json.dumps({"error": f"entity #{eid} não encontrada"})); return
        meta = entity["metadata"] if isinstance(entity["metadata"], dict) else json.loads(entity["metadata"] or "{}")
        existing = c.execute(text(
            "SELECT id, number FROM public.twin_invoices WHERE entity_id=:eid LIMIT 1"
        ), {"eid": eid}).mappings().first()
        if existing:
            print(json.dumps({"ok": True, "invoice_id": existing["id"], "number": existing["number"], "already_exists": True})); return
        valor = float(meta.get("valor_fatura") or 0)
        yr    = datetime.now().year
        cnt   = c.execute(text(
            "SELECT COUNT(*) FROM public.twin_invoices WHERE number LIKE :pat"
        ), {"pat": f"AIOS-{yr}-%"}).scalar()
        number = f"AIOS-{yr}-{cnt+1:04d}"
        due_date = (datetime.now() + timedelta(days=30)).date()
        case = c.execute(text(
            "SELECT id FROM public.twin_cases WHERE entity_id=:eid ORDER BY id DESC LIMIT 1"
        ), {"eid": eid}).mappings().first()
        row = c.execute(text("""
            INSERT INTO public.twin_invoices
              (entity_id, case_id, number, status, amount, client, due_date)
            VALUES (:eid, :cid, :num, 'issued', :amt, :cli, :due)
            RETURNING id
        """), {"eid": eid, "cid": case["id"] if case else None,
               "num": number, "amt": valor,
               "cli": meta.get("client"), "due": due_date}).fetchone()
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(),'info','twin','invoice_created',:eid,:msg,:data)"
        ), {"eid": eid, "msg": f"Fatura {number} emitida — €{valor:.2f}",
            "data": json.dumps({"invoice_number": number, "amount": valor, "entity_id": eid})})
    print(json.dumps({"ok": True, "invoice_id": row[0], "number": number,
                      "amount": valor, "due_date": str(due_date)}))


def cmd_finance_invoice_list(args):
    """Lista invoices. Args: [status] [limit=20]"""
    status = None
    limit  = 20
    for a in args:
        if a.isdigit():
            limit = int(a)
        elif a in ("issued", "paid", "overdue", "cancelled"):
            status = a
    engine, text = _conn()
    with engine.connect() as c:
        where = "WHERE i.status = :status" if status else ""
        rows = c.execute(text(f"""
            SELECT i.id, i.number, i.status, i.amount, i.currency, i.client,
                   i.due_date, i.paid_at, i.created_at,
                   e.name AS entity_name,
                   CASE WHEN i.status='issued' AND i.due_date < CURRENT_DATE
                        THEN 'overdue' ELSE i.status END AS effective_status
            FROM public.twin_invoices i
            JOIN public.twin_entities e ON e.id = i.entity_id
            {where}
            ORDER BY i.created_at DESC
            LIMIT :limit
        """), {"status": status, "limit": limit}).mappings().all()
    result = []
    for r in rows:
        d = dict(r)
        for k in ("due_date", "paid_at", "created_at"):
            if d.get(k) is not None:
                d[k] = str(d[k])
        if d.get("amount") is not None:
            d["amount"] = float(d["amount"])
        result.append(d)
    print(json.dumps({"ok": True, "invoices": result, "count": len(result)}))


def cmd_finance_invoice_pay(args):
    """Marca invoice como paga. Args: <invoice_id> [paid_at ISO]"""
    if not args:
        raise ValueError("usage: finance_invoice_pay <invoice_id> [paid_at]")
    inv_id = int(args[0])
    paid_at = args[1] if len(args) > 1 else None
    engine, text = _conn()
    with engine.begin() as c:
        inv = c.execute(text(
            "SELECT id, entity_id, number FROM public.twin_invoices WHERE id=:id"
        ), {"id": inv_id}).mappings().first()
        if not inv:
            print(json.dumps({"error": f"invoice #{inv_id} não encontrada"})); return
        ts = paid_at or datetime.now(timezone.utc).isoformat()
        c.execute(text(
            "UPDATE public.twin_invoices SET status='paid', paid_at=:ts, updated_at=NOW() WHERE id=:id"
        ), {"ts": ts, "id": inv_id})
        c.execute(text(
            "INSERT INTO public.events (ts, level, source, kind, entity_id, message, data) "
            "VALUES (NOW(),'info','twin','invoice_paid',:eid,:msg,:data)"
        ), {"eid": inv["entity_id"],
            "msg": f"Fatura {inv['number']} paga",
            "data": json.dumps({"invoice_id": inv_id, "number": inv["number"], "paid_at": ts})})
    print(json.dumps({"ok": True, "invoice_id": inv_id, "paid_at": ts}))


def cmd_finance_stats(_args):
    """Estatísticas financeiras globais."""
    engine, text = _conn()
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT
              COUNT(*) FILTER (WHERE status='issued' AND due_date >= CURRENT_DATE) AS pendente,
              COUNT(*) FILTER (WHERE status='issued' AND due_date < CURRENT_DATE)  AS vencido,
              COUNT(*) FILTER (WHERE status='paid')                                AS pago,
              COALESCE(SUM(amount) FILTER (WHERE status='issued'), 0)             AS total_pendente,
              COALESCE(SUM(amount) FILTER (WHERE status='paid'), 0)               AS total_pago,
              COALESCE(SUM(amount) FILTER (WHERE status='issued' AND due_date < CURRENT_DATE), 0) AS total_vencido
            FROM public.twin_invoices
        """)).mappings().first()
    print(json.dumps({"ok": True,
                      "pendente": int(row["pendente"]),
                      "vencido":  int(row["vencido"]),
                      "pago":     int(row["pago"]),
                      "total_pendente": float(row["total_pendente"]),
                      "total_pago":     float(row["total_pago"]),
                      "total_vencido":  float(row["total_vencido"])}))


# ── event timesheets ───────────────────────────────────────────────────────────

def cmd_timesheet_start(args):
    """Inicia timesheet: worker_id event_name [hourly_rate]"""
    if len(args) < 2:
        raise ValueError("worker_id e event_name obrigatórios")
    worker_id   = args[0]
    event_name  = args[1]
    hourly_rate = float(args[2]) if len(args) > 2 else None
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            INSERT INTO public.event_timesheets
              (worker_id, event_name, hourly_rate, start_time, status, updated_at)
            VALUES (:wid, :ev, :rate, NOW(), 'open', NOW())
            RETURNING id, start_time
        """), {"wid": worker_id, "ev": event_name, "rate": hourly_rate}).mappings().first()
    print(json.dumps({"ok": True, "id": row["id"],
                      "start_time": row["start_time"].isoformat()}))


def cmd_timesheet_stop(args):
    """Para timesheet: timesheet_id [notes]"""
    if not args:
        raise ValueError("timesheet_id obrigatório")
    ts_id = int(args[0])
    notes = args[1] if len(args) > 1 else None
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            UPDATE public.event_timesheets
            SET end_time  = NOW(),
                hours     = ROUND(EXTRACT(EPOCH FROM (NOW() - start_time))/3600.0, 2),
                notes     = COALESCE(:notes, notes),
                status    = 'submitted',
                updated_at = NOW()
            WHERE id = :id AND status = 'open'
            RETURNING id, hours, end_time, worker_id, event_name
        """), {"id": ts_id, "notes": notes}).mappings().first()
    if not row:
        print(json.dumps({"ok": False, "error": "Timesheet não encontrado ou já fechado"}))
        return
    print(json.dumps({"ok": True, "id": row["id"], "hours": float(row["hours"]),
                      "end_time": row["end_time"].isoformat(),
                      "worker_id": row["worker_id"], "event_name": row["event_name"]}))


def cmd_timesheet_list(args):
    """Lista timesheets: worker_id [status] [limit]"""
    if not args:
        raise ValueError("worker_id obrigatório")
    worker_id = args[0]
    status    = args[1] if len(args) > 1 and args[1] not in ('', 'all') else None
    limit     = int(args[2]) if len(args) > 2 else 30
    engine, text = _conn()
    with engine.connect() as c:
        if status:
            rows = c.execute(text("""
                SELECT id, worker_id, event_name, start_time, end_time, hours,
                       notes, status, hourly_rate, created_at
                FROM public.event_timesheets
                WHERE worker_id = :wid AND status = :st
                ORDER BY start_time DESC LIMIT :lim
            """), {"wid": worker_id, "st": status, "lim": limit}).mappings().all()
        else:
            rows = c.execute(text("""
                SELECT id, worker_id, event_name, start_time, end_time, hours,
                       notes, status, hourly_rate, created_at
                FROM public.event_timesheets
                WHERE worker_id = :wid
                ORDER BY start_time DESC LIMIT :lim
            """), {"wid": worker_id, "lim": limit}).mappings().all()
    print(json.dumps({"ok": True, "timesheets": [_row(r) for r in rows]}))


def cmd_timesheet_approve(args):
    """Aprova timesheet: timesheet_id"""
    if not args:
        raise ValueError("timesheet_id obrigatório")
    ts_id = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            UPDATE public.event_timesheets
            SET status = 'approved', updated_at = NOW()
            WHERE id = :id AND status = 'submitted'
            RETURNING id, worker_id, event_name, hours
        """), {"id": ts_id}).mappings().first()
    if not row:
        print(json.dumps({"ok": False, "error": "Timesheet não encontrado ou não submetido"}))
        return
    print(json.dumps({"ok": True, "id": row["id"], "worker_id": row["worker_id"],
                      "event_name": row["event_name"], "hours": float(row["hours"] or 0)}))


def cmd_timesheet_all(args):
    """Lista todos timesheets: [status] [limit]"""
    status = args[0] if args and args[0] not in ('', 'all') else None
    limit  = int(args[1]) if len(args) > 1 else 50
    engine, text = _conn()
    with engine.connect() as c:
        if status:
            rows = c.execute(text("""
                SELECT id, worker_id, event_name, start_time, end_time, hours,
                       notes, status, hourly_rate, created_at
                FROM public.event_timesheets
                WHERE status = :st
                ORDER BY start_time DESC LIMIT :lim
            """), {"st": status, "lim": limit}).mappings().all()
        else:
            rows = c.execute(text("""
                SELECT id, worker_id, event_name, start_time, end_time, hours,
                       notes, status, hourly_rate, created_at
                FROM public.event_timesheets
                ORDER BY start_time DESC LIMIT :lim
            """), {"lim": limit}).mappings().all()
    print(json.dumps({"ok": True, "timesheets": [_row(r) for r in rows]}))


# ── finance obligations ────────────────────────────────────────────────────────

def cmd_obligation_list(args):
    """Lista obrigações: [status] [days_ahead] [limit]"""
    status     = args[0] if args and args[0] not in ('', 'all') else None
    days_ahead = int(args[1]) if len(args) > 1 else 90
    limit      = int(args[2]) if len(args) > 2 else 50
    engine, text = _conn()
    with engine.connect() as c:
        if status:
            rows = c.execute(text("""
                SELECT id, type, entity, label, due_date, amount, status,
                       source, notes, paid_at, created_at,
                       (due_date - CURRENT_DATE) AS days_left
                FROM public.finance_obligations
                WHERE status = :st
                ORDER BY due_date ASC LIMIT :lim
            """), {"st": status, "lim": limit}).mappings().all()
        else:
            rows = c.execute(text("""
                SELECT id, type, entity, label, due_date, amount, status,
                       source, notes, paid_at, created_at,
                       (due_date - CURRENT_DATE) AS days_left
                FROM public.finance_obligations
                WHERE status != 'cancelled'
                  AND due_date >= CURRENT_DATE - INTERVAL '30 days'
                  AND due_date <= CURRENT_DATE + (:days * INTERVAL '1 day')
                ORDER BY due_date ASC LIMIT :lim
            """), {"days": days_ahead, "lim": limit}).mappings().all()
    result = []
    for r in rows:
        d = _row(r)
        d["days_left"] = int(r["days_left"]) if r["days_left"] is not None else None
        result.append(d)
    print(json.dumps({"ok": True, "obligations": result}))


def cmd_obligation_pay(args):
    """Marca obrigação como paga: obligation_id [paid_at]"""
    if not args:
        raise ValueError("obligation_id obrigatório")
    ob_id   = int(args[0])
    paid_at = args[1] if len(args) > 1 else None
    ts      = paid_at or datetime.now(timezone.utc).isoformat()
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            UPDATE public.finance_obligations
            SET status = 'paid', paid_at = :ts, updated_at = NOW()
            WHERE id = :id AND status = 'pending'
            RETURNING id, label, due_date, amount
        """), {"id": ob_id, "ts": ts}).mappings().first()
        if row:
            c.execute(text(
                "INSERT INTO public.events (ts,level,source,kind,message,data) "
                "VALUES (NOW(),'info','finance','obligation_paid',:msg,:data)"
            ), {"msg": f"Obrigação paga: {row['label']}",
                "data": json.dumps({"obligation_id": ob_id, "label": row["label"],
                                    "due_date": str(row["due_date"]), "paid_at": ts})})
    if not row:
        print(json.dumps({"ok": False, "error": "Obrigação não encontrada ou já paga"}))
        return
    print(json.dumps({"ok": True, "id": row["id"], "label": row["label"],
                      "due_date": str(row["due_date"]),
                      "amount": float(row["amount"]) if row["amount"] else None,
                      "paid_at": ts}))


def cmd_obligation_stats(_args):
    """Estatísticas de obrigações fiscais."""
    engine, text = _conn()
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT
              COUNT(*) FILTER (WHERE status='pending' AND due_date < CURRENT_DATE)      AS overdue,
              COUNT(*) FILTER (WHERE status='pending' AND due_date = CURRENT_DATE)      AS today,
              COUNT(*) FILTER (WHERE status='pending' AND due_date BETWEEN CURRENT_DATE+1 AND CURRENT_DATE+7) AS week,
              COUNT(*) FILTER (WHERE status='pending' AND due_date BETWEEN CURRENT_DATE+8 AND CURRENT_DATE+30) AS month,
              COUNT(*) FILTER (WHERE status='paid')                                     AS paid,
              COALESCE(SUM(amount) FILTER (WHERE status='pending'), 0)                 AS total_pending
            FROM public.finance_obligations
        """)).mappings().first()
    # próxima obrigação
    next_row = c.execute(text("""
        SELECT label, due_date, (due_date - CURRENT_DATE) AS days_left
        FROM public.finance_obligations
        WHERE status='pending' AND due_date >= CURRENT_DATE
        ORDER BY due_date ASC LIMIT 1
    """)).mappings().first() if False else None
    with engine.connect() as c2:
        next_row = c2.execute(text("""
            SELECT label, due_date, (due_date - CURRENT_DATE) AS days_left
            FROM public.finance_obligations
            WHERE status='pending' AND due_date >= CURRENT_DATE
            ORDER BY due_date ASC LIMIT 1
        """)).mappings().first()
    print(json.dumps({
        "ok": True,
        "overdue": int(row["overdue"]),
        "today":   int(row["today"]),
        "week":    int(row["week"]),
        "month":   int(row["month"]),
        "paid":    int(row["paid"]),
        "total_pending": float(row["total_pending"]),
        "next": {"label": next_row["label"],
                 "due_date": str(next_row["due_date"]),
                 "days_left": int(next_row["days_left"])} if next_row else None
    }))


# ── obligation approve ────────────────────────────────────────────────────────

def cmd_obligation_approve(args):
    """Aprova obrigação para pagamento: obligation_id"""
    if not args:
        raise ValueError("obligation_id obrigatório")
    ob_id = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            UPDATE public.finance_obligations
            SET status = 'approved', updated_at = NOW()
            WHERE id = :id AND status = 'pending'
            RETURNING id, label, due_date, amount
        """), {"id": ob_id}).mappings().first()
    if not row:
        print(json.dumps({"ok": False, "error": "Obrigação não encontrada ou já aprovada"}))
        return
    print(json.dumps({"ok": True, "id": row["id"], "label": row["label"],
                      "due_date": str(row["due_date"]), "amount": float(row["amount"] or 0)}))


# ── people ────────────────────────────────────────────────────────────────────

def cmd_people_list(args):
    """Lista técnicos: [cluster] [active_only]"""
    cluster     = args[0] if args and args[0] not in ('', 'all') else None
    active_only = args[1].lower() not in ('0', 'false', 'no') if len(args) > 1 else True
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, name, role, cluster, hourly_rate, phone, email, active, created_at
            FROM public.people
            WHERE (:cluster IS NULL OR cluster = :cluster)
              AND (:active IS FALSE OR active = TRUE)
            ORDER BY name
        """), {"cluster": cluster, "active": active_only}).mappings().all()
    print(json.dumps({"ok": True, "people": [_row(r) for r in rows]}))


# ── clients ───────────────────────────────────────────────────────────────────

def cmd_client_list(args):
    """Lista clientes: [active_only]"""
    active_only = args[0].lower() not in ('0', 'false', 'no') if args else True
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, company_name, nif, billing_email, contact_name, phone, token, active, created_at
            FROM public.clients
            WHERE (:active IS FALSE OR active = TRUE)
            ORDER BY company_name
        """), {"active": active_only}).mappings().all()
    print(json.dumps({"ok": True, "clients": [_row(r) for r in rows]}))


# ── client portal — timesheets ────────────────────────────────────────────────

def cmd_client_timesheets(args):
    """Timesheets de cliente via token: token [status] [limit]"""
    if not args:
        raise ValueError("token obrigatório")
    token  = args[0]
    status = args[1] if len(args) > 1 and args[1] not in ('', 'all') else None
    limit  = int(args[2]) if len(args) > 2 else 50
    engine, text = _conn()
    with engine.connect() as c:
        client = c.execute(text("""
            SELECT id, company_name FROM public.clients WHERE token = :t AND active = TRUE
        """), {"t": token}).mappings().first()
        if not client:
            print(json.dumps({"ok": False, "error": "Token inválido"}))
            return
        # timesheets where event_id maps to a case linked to this client
        # simplified: return all timesheets (MVP — sem filtro por cliente ainda)
        rows = c.execute(text("""
            SELECT id, worker_id, event_name, start_time, end_time, hours,
                   notes, status, hourly_rate, created_at, updated_at
            FROM public.event_timesheets
            WHERE (:status IS NULL OR status = :status)
              AND status != 'open'
            ORDER BY created_at DESC LIMIT :lim
        """), {"status": status, "lim": limit}).mappings().all()
    print(json.dumps({
        "ok": True,
        "client": {"id": client["id"], "name": client["company_name"]},
        "timesheets": [_row(r) for r in rows]
    }))


def cmd_client_timesheet_action(args):
    """Aprova/rejeita timesheet via token cliente: token timesheet_id approved|rejected"""
    if len(args) < 3:
        raise ValueError("token, timesheet_id e action (approved|rejected) obrigatórios")
    token  = args[0]
    ts_id  = int(args[1])
    action = args[2]
    if action not in ('approved', 'rejected'):
        raise ValueError("action deve ser approved ou rejected")
    engine, text = _conn()
    with engine.begin() as c:
        client = c.execute(text("""
            SELECT id, company_name FROM public.clients WHERE token = :t AND active = TRUE
        """), {"t": token}).mappings().first()
        if not client:
            print(json.dumps({"ok": False, "error": "Token inválido"}))
            return
        row = c.execute(text("""
            UPDATE public.event_timesheets
            SET status = :action, updated_at = NOW()
            WHERE id = :id AND status = 'submitted'
            RETURNING id, worker_id, event_name, hours
        """), {"action": action, "id": ts_id}).mappings().first()
        if not row:
            print(json.dumps({"ok": False, "error": "Timesheet não encontrado ou não submetido"}))
            return
        # registar evento
        kind = "timesheet_approved" if action == "approved" else "timesheet_rejected"
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'client_portal', :kind,
                    :msg, CAST(:data AS jsonb))
        """), {
            "kind": kind,
            "msg":  f"{kind}: {row['worker_id']} — {row['event_name']} ({row['hours']}h)",
            "data": json.dumps({"ts_id": ts_id, "client_id": client["id"],
                                "worker_id": row["worker_id"], "hours": float(row["hours"] or 0)})
        })
    print(json.dumps({"ok": True, "id": row["id"], "status": action,
                      "worker_id": row["worker_id"], "hours": float(row["hours"] or 0)}))


# ── payouts ───────────────────────────────────────────────────────────────────

def cmd_payout_list(args):
    """Lista payouts: [status] [limit]"""
    status = args[0] if args and args[0] not in ('', 'all') else None
    limit  = int(args[1]) if len(args) > 1 else 50
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, worker_id, week_start, total_hours, amount, status, paid_at, notes, created_at
            FROM public.finance_payouts
            WHERE (:status IS NULL OR status = :status)
            ORDER BY week_start DESC, worker_id
            LIMIT :lim
        """), {"status": status, "lim": limit}).mappings().all()
    print(json.dumps({"ok": True, "payouts": [_row(r) for r in rows]}))


def cmd_payout_run(args):
    """Calcula payouts para semana: week_start (YYYY-MM-DD). Cria se não existir."""
    if not args:
        raise ValueError("week_start obrigatório (YYYY-MM-DD)")
    week_start = args[0]
    engine, text = _conn()
    with engine.begin() as c:
        # horas aprovadas não pagas por worker nesta semana
        rows = c.execute(text("""
            SELECT t.worker_id,
                   COALESCE(p.hourly_rate, t.hourly_rate, 0) AS rate,
                   SUM(t.hours) AS total_hours
            FROM public.event_timesheets t
            LEFT JOIN public.people p ON p.name = t.worker_id
            WHERE t.status IN ('approved')
              AND t.start_time >= CAST(:week_start AS date)
              AND t.start_time <  CAST(:week_start AS date) + INTERVAL '7 days'
            GROUP BY t.worker_id, p.hourly_rate, t.hourly_rate
        """), {"week_start": week_start}).mappings().all()
        created = []
        for r in rows:
            hours  = float(r["total_hours"] or 0)
            rate   = float(r["rate"] or 0)
            amount = round(hours * rate, 2)
            existing = c.execute(text("""
                SELECT id FROM public.finance_payouts
                WHERE worker_id = :wid AND week_start = :ws
            """), {"wid": r["worker_id"], "ws": week_start}).first()
            if existing:
                c.execute(text("""
                    UPDATE public.finance_payouts
                    SET total_hours = :h, amount = :a, updated_at = NOW()
                    WHERE id = :id
                """), {"h": hours, "a": amount, "id": existing[0]})
                created.append({"worker_id": r["worker_id"], "hours": hours, "amount": amount, "updated": True})
            else:
                c.execute(text("""
                    INSERT INTO public.finance_payouts (worker_id, week_start, total_hours, amount)
                    VALUES (:wid, :ws, :h, :a)
                """), {"wid": r["worker_id"], "ws": week_start, "h": hours, "a": amount})
                created.append({"worker_id": r["worker_id"], "hours": hours, "amount": amount, "updated": False})
        total = sum(p["amount"] for p in created)
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'payout_run', 'payout_run_created',
                    :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Payout run {week_start}: {len(created)} workers, total {total:.2f}€",
            "data": json.dumps({"week_start": week_start, "workers": created, "total": total})
        })
    print(json.dumps({"ok": True, "week_start": week_start, "payouts": created, "total": total}))


def cmd_payout_mark_paid(args):
    """Marca payout como pago e as timesheets associadas como 'paid': payout_id"""
    if not args:
        raise ValueError("payout_id obrigatório")
    p_id = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            UPDATE public.finance_payouts
            SET status = 'paid', paid_at = NOW(), updated_at = NOW()
            WHERE id = :id AND status != 'paid'
            RETURNING id, worker_id, week_start, amount
        """), {"id": p_id}).mappings().first()
        if not row:
            print(json.dumps({"ok": False, "error": "Payout não encontrado ou já pago"}))
            return
        # Marcar timesheets aprovadas do worker nessa semana como 'paid'
        ts_updated = c.execute(text("""
            UPDATE public.event_timesheets
            SET status = 'paid', updated_at = NOW()
            WHERE LOWER(worker_id) = LOWER(:wid)
              AND status = 'approved'
              AND DATE(created_at) >= CAST(:ws AS date)
              AND DATE(created_at) <  CAST(:ws AS date) + INTERVAL '7 days'
        """), {
            "wid": row["worker_id"],
            "ws":  str(row["week_start"]),
        }).rowcount
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'payout', 'payout_paid',
                    :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Payout #{row['id']} marcado pago — {row['worker_id']} {float(row['amount']):.2f}€",
            "data": json.dumps({"payout_id": row["id"], "worker_id": row["worker_id"],
                                "timesheets_updated": ts_updated}),
        })
    print(json.dumps({"ok": True, "id": row["id"], "worker_id": row["worker_id"],
                      "week_start": str(row["week_start"]), "amount": float(row["amount"]),
                      "timesheets_paid": ts_updated}))


# ── invoice engine ────────────────────────────────────────────────────────────

def _import_invoice_engine():
    """Importa invoice_engine adicionando o directório pai ao path."""
    import sys as _s, os as _o
    _parent = _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__)))
    if _parent not in _s.path:
        _s.path.insert(0, _parent)
    import importlib
    return importlib.import_module("bin.invoice_engine")


def cmd_invoice_generate(args):
    """Gera invoice draft a partir de timesheets aprovadas: event_name [client_id]"""
    if not args:
        raise ValueError("event_name obrigatório")
    event_name = args[0]
    client_id  = int(args[1]) if len(args) > 1 else None
    m = _import_invoice_engine()
    print(json.dumps(m.generate_from_timesheets(event_name, client_id), ensure_ascii=False))


def cmd_invoice_push_toc(args):
    """Envia invoice para Toconline (draft): invoice_id"""
    if not args:
        raise ValueError("invoice_id obrigatório")
    m = _import_invoice_engine()
    print(json.dumps(m.push_to_toconline(int(args[0])), ensure_ascii=False))


def cmd_invoice_drafts(_args):
    """Lista invoices em draft."""
    m = _import_invoice_engine()
    print(json.dumps(m.list_drafts(), ensure_ascii=False))


def cmd_toconline_status(_args):
    """Estado da ligação Toconline."""
    m = _import_invoice_engine()
    print(json.dumps(m.toconline_status(), ensure_ascii=False))


def cmd_timesheet_submit(args):
    """Submete timesheet parada (stop + submit numa só chamada): timesheet_id [notes]"""
    if not args:
        raise ValueError("timesheet_id obrigatório")
    ts_id = int(args[0])
    notes = args[1] if len(args) > 1 else None
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            UPDATE public.event_timesheets
            SET end_time   = COALESCE(end_time, NOW()),
                hours      = COALESCE(hours, ROUND(EXTRACT(EPOCH FROM (COALESCE(end_time,NOW()) - start_time))/3600.0, 2)),
                notes      = COALESCE(:notes, notes),
                status     = 'submitted',
                updated_at = NOW()
            WHERE id = :id AND status IN ('open', 'submitted')
            RETURNING id, worker_id, event_name, hours, status
        """), {"id": ts_id, "notes": notes}).mappings().first()
    if not row:
        print(json.dumps({"ok": False, "error": "Timesheet não encontrado"}))
        return
    print(json.dumps({"ok": True, "id": row["id"], "worker_id": row["worker_id"],
                      "event_name": row["event_name"], "hours": float(row["hours"] or 0),
                      "status": row["status"]}))


# ── ideas (Conselho de IA / Painel do João) ───────────────────────────────────

def cmd_idea_create(args):
    """Cria thread de ideia: title [message]"""
    if not args:
        raise ValueError("title obrigatório")
    title   = args[0]
    message = args[1] if len(args) > 1 else None
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            INSERT INTO public.idea_threads (title) VALUES (:t) RETURNING id, title, status, created_at
        """), {"t": title}).mappings().first()
        tid = row["id"]
        if message:
            c.execute(text("""
                INSERT INTO public.idea_messages (thread_id, role, content)
                VALUES (:tid, 'joao', :content)
            """), {"tid": tid, "content": message})
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'joao', 'idea_captured',
                    :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Ideia capturada: {title}",
            "data": json.dumps({"thread_id": tid, "title": title})
        })
        if message:
            # auto-enqueue cluster analysis when idea has a message
            c.execute(text(
                "INSERT INTO public.worker_jobs (ts_created, status, kind, payload) "
                "VALUES (NOW(), 'queued', 'ai_analysis', :payload)"
            ), {"payload": json.dumps({"thread_id": tid})})
    print(json.dumps({"ok": True, "id": tid, "title": title,
                      "status": row["status"], "created_at": row["created_at"].isoformat()}))


def cmd_idea_list(args):
    """Lista ideias: [status] [limit]"""
    # args[0] pode ser status (texto) ou limit (número) quando status omitido
    if args and args[0].lstrip('-').isdigit():
        status, limit = None, int(args[0])
    else:
        status = args[0] if args and args[0] not in ('', 'all', '_') else None
        limit  = int(args[1]) if len(args) > 1 else 30
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT t.id, t.title, t.source, t.status, t.created_at, t.updated_at,
                   (SELECT content FROM public.idea_messages
                    WHERE thread_id=t.id AND role='joao'
                    ORDER BY created_at LIMIT 1) AS first_msg,
                   (SELECT COUNT(*) FROM public.idea_reviews WHERE thread_id=t.id) AS review_count,
                   (SELECT AVG(score)::integer FROM public.idea_reviews
                    WHERE thread_id=t.id AND score IS NOT NULL AND agent != 'system') AS avg_score
            FROM public.idea_threads t
            WHERE (:status IS NULL OR t.status = :status)
            ORDER BY t.created_at DESC LIMIT :lim
        """), {"status": status, "lim": limit}).mappings().all()
    print(json.dumps({"ok": True, "ideas": [_row(r) for r in rows]}))


def cmd_idea_get(args):
    """Obtém thread completo: thread_id"""
    if not args:
        raise ValueError("thread_id obrigatório")
    tid = int(args[0])
    engine, text = _conn()
    with engine.connect() as c:
        thread = c.execute(text(
            "SELECT id, title, source, status, created_at, updated_at FROM public.idea_threads WHERE id=:id"
        ), {"id": tid}).mappings().first()
        if not thread:
            print(json.dumps({"ok": False, "error": "Thread não encontrado"}))
            return
        msgs = c.execute(text("""
            SELECT id, role, content, created_at FROM public.idea_messages
            WHERE thread_id=:tid ORDER BY created_at
        """), {"tid": tid}).mappings().all()
        reviews = c.execute(text("""
            SELECT id, agent, summary, risks, next_steps, score, raw, created_at
            FROM public.idea_reviews WHERE thread_id=:tid ORDER BY created_at
        """), {"tid": tid}).mappings().all()
        # caso associado (se ideia já foi convertida em projeto)
        case_row = c.execute(text("""
            SELECT tc.id AS case_id, tc.status AS case_status
            FROM public.twin_entities te
            JOIN public.twin_cases tc ON tc.entity_id = te.id
            WHERE te.type='idea' AND te.metadata->>'idea_thread_id' = :tid
            ORDER BY tc.id DESC LIMIT 1
        """), {"tid": str(tid)}).mappings().first()
        tasks = []
        if case_row:
            tasks = c.execute(text("""
                SELECT id, title, status FROM public.twin_tasks
                WHERE case_id=:cid ORDER BY id
            """), {"cid": case_row["case_id"]}).mappings().all()
    print(json.dumps({"ok": True, "thread": _row(thread),
                      "messages": [_row(m) for m in msgs],
                      "reviews":  [_row(r) for r in reviews],
                      "case":     _row(case_row) if case_row else None,
                      "tasks":    [_row(t) for t in tasks]}))


def cmd_idea_archive(args):
    """Arquiva ideia: thread_id"""
    if not args:
        raise ValueError("thread_id obrigatório")
    tid = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        c.execute(text("""
            UPDATE public.idea_threads SET status='archived', updated_at=NOW() WHERE id=:id
        """), {"id": tid})
    print(json.dumps({"ok": True, "id": tid, "status": "archived"}))


def cmd_idea_reviews(args):
    """Reviews de uma ideia: thread_id"""
    if not args:
        raise ValueError("thread_id obrigatório")
    tid = int(args[0])
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, agent, summary, risks, next_steps, score, raw, created_at
            FROM public.idea_reviews WHERE thread_id=:tid ORDER BY created_at
        """), {"tid": tid}).mappings().all()
    print(json.dumps({"ok": True, "thread_id": tid, "reviews": [_row(r) for r in rows]}))


def cmd_idea_create_case(args):
    """Cria case Twin a partir de ideia: thread_id"""
    if not args:
        raise ValueError("thread_id obrigatório")
    tid = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        thread = c.execute(text(
            "SELECT id, title FROM public.idea_threads WHERE id=:id"
        ), {"id": tid}).mappings().first()
        if not thread:
            print(json.dumps({"ok": False, "error": "Thread não encontrado"}))
            return
        # idempotência: devolver case existente se já foi criado
        existing = c.execute(text("""
            SELECT te.id AS entity_id, tc.id AS case_id
            FROM public.twin_entities te
            JOIN public.twin_cases tc ON tc.entity_id = te.id
            WHERE te.type='idea' AND te.metadata->>'idea_thread_id' = :tid
            ORDER BY tc.id DESC LIMIT 1
        """), {"tid": str(tid)}).mappings().first()
        if existing:
            print(json.dumps({"ok": True, "thread_id": tid,
                              "entity_id": existing["entity_id"],
                              "case_id": existing["case_id"],
                              "status": "project", "existing": True}))
            return
        # criar entity de tipo idea
        ent = c.execute(text("""
            INSERT INTO public.twin_entities (type, name, status, metadata)
            VALUES ('idea', :name, 'active', CAST(:meta AS jsonb))
            RETURNING id
        """), {"name": thread["title"],
               "meta": json.dumps({"idea_thread_id": tid})}).mappings().first()
        ent_id = ent["id"]
        # workflow idea_case (inserir se não existir)
        c.execute(text("""
            INSERT INTO public.twin_workflows (key, name, definition)
            VALUES ('idea_case', 'Ideia → Caso', '{"states":["captured","analyzing","decided","project","archived"]}')
            ON CONFLICT (key) DO NOTHING
        """))
        # criar case
        case = c.execute(text("""
            INSERT INTO public.twin_cases (workflow_key, entity_id, status, data)
            VALUES ('idea_case', :eid, 'captured', CAST(:data AS jsonb))
            RETURNING id
        """), {"eid": ent_id,
               "data": json.dumps({"idea_thread_id": tid, "title": thread["title"]})}).mappings().first()
        case_id = case["id"]
        # tasks iniciais
        for t in ["Clarificar objetivo", "Levantar custos", "Identificar parceiros", "Definir próximo passo"]:
            c.execute(text("""
                INSERT INTO public.twin_tasks (case_id, title, type, status)
                VALUES (:cid, :title, 'human', 'pending')
            """), {"cid": case_id, "title": t})
        # eventos
        c.execute(text("""
            INSERT INTO public.events (ts, level, source, kind, message, data)
            VALUES (NOW(), 'info', 'joao', 'idea_case_created',
                    :msg, CAST(:data AS jsonb))
        """), {
            "msg":  f"Case criado para ideia: {thread['title']}",
            "data": json.dumps({"thread_id": tid, "entity_id": ent_id, "case_id": case_id})
        })
        # marcar thread
        c.execute(text("""
            UPDATE public.idea_threads SET status='project', updated_at=NOW() WHERE id=:id
        """), {"id": tid})
        # decisão
        c.execute(text("""
            INSERT INTO public.decision_queue (kind, ref_id, title)
            VALUES ('idea_case', :ref, :title)
        """), {"ref": str(case_id), "title": f"Rever case: {thread['title']}"})
    print(json.dumps({"ok": True, "thread_id": tid, "entity_id": ent_id,
                      "case_id": case_id, "status": "project"}))


# ── decision queue ────────────────────────────────────────────────────────────

def cmd_decision_list(args):
    """Lista decisões pendentes: [status] [limit]"""
    status = args[0] if args and args[0] not in ('', 'all') else 'pending'
    limit  = int(args[1]) if len(args) > 1 else 20
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, kind, ref_id, title, status, created_at
            FROM public.decision_queue
            WHERE (:status IS NULL OR status = :status)
            ORDER BY created_at DESC LIMIT :lim
        """), {"status": status if status != 'all' else None, "lim": limit}).mappings().all()
    print(json.dumps({"ok": True, "decisions": [_row(r) for r in rows]}))


def cmd_decision_create(args):
    """Cria decisão manual: title [kind]"""
    if not args:
        raise ValueError("title obrigatório")
    title = args[0]
    kind  = args[1] if len(args) > 1 else 'manual'
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text("""
            INSERT INTO public.decision_queue (kind, title, status)
            VALUES (:kind, :title, 'pending')
            RETURNING id, kind, title, status, created_at
        """), {"kind": kind, "title": title}).mappings().first()
    print(json.dumps({"ok": True, **_row(row)}))


def cmd_decision_resolve(args):
    """Resolve decisão: decision_id"""
    if not args:
        raise ValueError("decision_id obrigatório")
    did = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        c.execute(text("""
            UPDATE public.decision_queue SET status='resolved', updated_at=NOW() WHERE id=:id
        """), {"id": did})
    print(json.dumps({"ok": True, "id": did, "status": "resolved"}))


# ── dispatch ──────────────────────────────────────────────────────────────────

# ── agent_suggestions ────────────────────────────────────────────────────────

def cmd_agent_suggestions(args):
    """agent_suggestions [kind] [limit]"""
    kind  = args[0] if args else None
    limit = int(args[1]) if len(args) > 1 else 30
    engine, text = _conn()
    with engine.connect() as c:
        q = """
            SELECT id, kind, title, details, ref_kind, ref_id, score, is_read, created_at
            FROM public.agent_suggestions
            WHERE (:kind IS NULL OR kind = :kind)
            ORDER BY created_at DESC LIMIT :lim
        """
        rows = c.execute(text(q), {"kind": kind, "lim": limit}).mappings().all()
    print(json.dumps({"ok": True, "suggestions": [_row(r) for r in rows]}, ensure_ascii=False))


def cmd_agent_briefing(_args):
    """agent_briefing — latest briefing text"""
    engine, text = _conn()
    with engine.connect() as c:
        row = c.execute(text("""
            SELECT id, title, details, created_at
            FROM public.agent_suggestions
            WHERE kind = 'briefing'
            ORDER BY created_at DESC LIMIT 1
        """)).mappings().first()
    if not row:
        print(json.dumps({"ok": True, "briefing": None}))
        return
    print(json.dumps({"ok": True, "briefing": _row(row)}, ensure_ascii=False))


def cmd_agent_suggestion_read(args):
    """agent_suggestion_read <id>"""
    if not args:
        print(json.dumps({"ok": False, "error": "id required"})); return
    sid = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        c.execute(text("UPDATE public.agent_suggestions SET is_read=TRUE WHERE id=:id"), {"id": sid})
    print(json.dumps({"ok": True, "id": sid}))


# ── bank reconciliation (proxy para reconcile.py) ────────────────────────────

def _run_reconcile(subcmd: str, args: list) -> dict:
    import subprocess, shlex
    cmd = ["python3", os.path.join(os.path.dirname(__file__), "reconcile.py"), subcmd] + [str(a) for a in args]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=os.path.dirname(os.path.dirname(__file__)))
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"error": r.stderr.strip() or "reconcile error"}


def cmd_bank_transactions(args):
    print(json.dumps(_run_reconcile("bank_transactions", args), ensure_ascii=False))


def cmd_bank_reconcile(args):
    print(json.dumps(_run_reconcile("bank_reconcile", args), ensure_ascii=False))


def cmd_bank_match(args):
    print(json.dumps(_run_reconcile("bank_match", args), ensure_ascii=False))


def cmd_bank_ignore(args):
    print(json.dumps(_run_reconcile("bank_ignore", args), ensure_ascii=False))


# ── incidents ──────────────────────────────────────────────────────────────────

def cmd_incident_list(args):
    """incident_list [limit=50] — lista incidentes activos"""
    limit = int(args[0]) if args and args[0].isdigit() else 50
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT id, source, kind, severity, title, details, status, resolved_at, created_at
            FROM public.incidents
            WHERE status = 'open'
            ORDER BY
              CASE severity WHEN 'crit' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END,
              created_at DESC
            LIMIT :l
        """), {"l": limit}).mappings().all()
    print(json.dumps({"ok": True, "incidents": [_row(r) for r in rows]}, ensure_ascii=False))


def cmd_incident_resolve(args):
    """incident_resolve <id>"""
    if not args:
        print(json.dumps({"error": "usage: incident_resolve <id>"})); return
    iid = int(args[0])
    engine, text = _conn()
    with engine.begin() as c:
        c.execute(text("""
            UPDATE public.incidents SET status='resolved', resolved_at=NOW()
            WHERE id=:id
        """), {"id": iid})
    print(json.dumps({"ok": True, "id": iid}))


def cmd_incident_create(args):
    """incident_create <source> <kind> <severity> <title> [details]"""
    if len(args) < 4:
        print(json.dumps({"error": "usage: incident_create <source> <kind> <severity> <title> [details]"})); return
    source   = args[0]
    kind     = args[1]
    severity = args[2] if args[2] in ("info","warn","crit") else "warn"
    title    = args[3]
    details  = args[4] if len(args) > 4 else None
    engine, text = _conn()
    with engine.begin() as c:
        # Dedupe: não criar se já existe incidente open com mesmo kind+source nas últimas 4h
        existing = c.execute(text("""
            SELECT id FROM public.incidents
            WHERE source=:s AND kind=:k AND status='open'
              AND created_at > NOW() - INTERVAL '4 hours'
            LIMIT 1
        """), {"s": source, "k": kind}).mappings().first()
        if existing:
            print(json.dumps({"ok": True, "id": existing["id"], "deduped": True})); return
        row = c.execute(text("""
            INSERT INTO public.incidents (source, kind, severity, title, details)
            VALUES (:source, :kind, :severity, :title, :details)
            RETURNING id
        """), {"source": source, "kind": kind, "severity": severity,
               "title": title, "details": details}).mappings().first()
    print(json.dumps({"ok": True, "id": row["id"]}))


# ── Pipeline helpers ───────────────────────────────────────────────────────────

def cmd_cluster_metrics(args):
    """Latest telemetry per node. Args: [limit=6]"""
    limit = int(args[0]) if args else 6
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT DISTINCT ON (node)
                node, ip, cpu_pct, ram_used_mb, ram_total_mb,
                load_1, disk_used_pct, worker_state, current_job_id,
                node_role, jobs_24h, failures_24h, created_at
            FROM public.cluster_node_metrics
            ORDER BY node, created_at DESC
            LIMIT :n
        """), {"n": limit}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


def cmd_agent_status(_args):
    """Estado por agente/role: last 24h jobs, falhas, último trabalho. Usa cluster_workers.json para nomes."""
    import pathlib as _pl
    _ROOT = os.path.dirname(_bin_dir)
    cfg_path = os.path.join(_ROOT, "config", "cluster_workers.json")
    try:
        cfg = json.loads(_pl.Path(cfg_path).read_text())
        nodes_cfg = cfg.get("nodes", {})
    except Exception:
        nodes_cfg = {}

    engine, text = _conn()
    with engine.connect() as c:
        workers = c.execute(text("""
            SELECT id, hostname, role, status, last_seen,
                   EXTRACT(EPOCH FROM (NOW() - last_seen))::int AS age_secs
            FROM public.workers
            ORDER BY last_seen DESC NULLS LAST
        """)).mappings().all()

        job_stats = c.execute(text("""
            SELECT assigned_worker_id AS wid,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE status='done')    AS done_cnt,
                   COUNT(*) FILTER (WHERE status='failed')  AS fail_cnt,
                   COUNT(*) FILTER (WHERE status='running') AS running_cnt,
                   MAX(ts_done) AS last_done,
                   (ARRAY_AGG(kind ORDER BY ts_created DESC))[1] AS last_kind
            FROM public.worker_jobs
            WHERE ts_created > NOW() - INTERVAL '24 hours'
              AND assigned_worker_id IS NOT NULL
            GROUP BY assigned_worker_id
        """)).mappings().all()

    job_map = {r["wid"]: dict(r) for r in job_stats}
    result = []
    for w in workers:
        wid  = w["id"]
        ncfg = nodes_cfg.get(wid, {})
        js   = job_map.get(wid, {})
        result.append({
            "node":        wid,
            "agent":       ncfg.get("agent", wid),
            "agent_desc":  ncfg.get("agent_desc", ""),
            "roles":       w["role"] or "",
            "status":      w["status"] or "unknown",
            "age_secs":    w["age_secs"],
            "jobs_24h":    int(js.get("total") or 0),
            "jobs_done":   int(js.get("done_cnt") or 0),
            "jobs_failed": int(js.get("fail_cnt") or 0),
            "running":     int(js.get("running_cnt") or 0),
            "last_kind":   js.get("last_kind"),
            "last_job_at": _row({"t": js["last_done"]})["t"] if js.get("last_done") else None,
        })
    print(json.dumps(result, ensure_ascii=False))


def cmd_pipeline_idea_analyze(args):
    """Enfileira análise de ideia no cluster. Args: <thread_id>"""
    if not args:
        raise ValueError("usage: pipeline_idea_analyze <thread_id>")
    thread_id = int(args[0])
    engine, text = _conn()
    with engine.connect() as c:
        row = c.execute(text(
            "SELECT id FROM public.idea_threads WHERE id=:id"
        ), {"id": thread_id}).mappings().first()
    if not row:
        raise ValueError(f"Thread {thread_id} não existe")
    with engine.begin() as c:
        row = c.execute(text(
            "INSERT INTO public.worker_jobs (ts_created, status, kind, payload) "
            "VALUES (NOW(), 'queued', 'ai_analysis', :payload) RETURNING id"
        ), {"payload": json.dumps({"thread_id": thread_id})})
        job_id = row.scalar()
    print(json.dumps({"ok": True, "job_id": job_id, "thread_id": thread_id}))


def cmd_pipeline_radar_score(args):
    """Enfileira scoring de radar no cluster. Args: [source=ted]"""
    source = args[0] if args else "ted"
    if source not in ("ted", "base", "dr"):
        raise ValueError("source deve ser ted, base ou dr")
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text(
            "INSERT INTO public.worker_jobs (ts_created, status, kind, payload) "
            "VALUES (NOW(), 'queued', 'radar', :payload) RETURNING id"
        ), {"payload": json.dumps({"script": "radar_score", "args": ["--source", source]})})
        job_id = row.scalar()
    print(json.dumps({"ok": True, "job_id": job_id, "source": source}))


def cmd_pipeline_incidents(args):
    """Enfileira verificação de incidentes no cluster."""
    root = os.environ.get("AIOS_CLUSTER_ROOT", "/cluster/d1/ai-os")
    engine, text = _conn()
    with engine.begin() as c:
        row = c.execute(text(
            "INSERT INTO public.worker_jobs (ts_created, status, kind, payload) "
            "VALUES (NOW(), 'queued', 'automation', :payload) RETURNING id"
        ), {"payload": json.dumps({"cmd": f"python3 {root}/bin/incidents_tick.py"})})
        job_id = row.scalar()
    print(json.dumps({"ok": True, "job_id": job_id}))


# ── Document Vault ───────────────────────────────────────────────────────────

def cmd_doc_summary(args):
    """Resumo documental: contagens por status + pedidos abertos."""
    engine, text = _conn()
    with engine.connect() as c:
        counts = c.execute(text("""
            SELECT status, COUNT(*) AS n FROM public.documents GROUP BY status
        """)).mappings().all()
        requests = c.execute(text("""
            SELECT status, COUNT(*) AS n FROM public.document_requests GROUP BY status
        """)).mappings().all()
        critical = c.execute(text("""
            SELECT d.id, d.title, d.doc_type, d.owner_type, d.owner_id,
                   d.expiry_date, d.status, d.sensitivity
            FROM public.documents d
            WHERE d.status IN ('expired','expiring')
            ORDER BY d.expiry_date ASC NULLS LAST
            LIMIT 10
        """)).mappings().all()
    doc_counts = {r["status"]: r["n"] for r in counts}
    req_counts = {r["status"]: r["n"] for r in requests}
    print(json.dumps({
        "doc_counts":    doc_counts,
        "req_counts":    req_counts,
        "critical_docs": [_row(r) for r in critical],
    }, ensure_ascii=False))


def cmd_doc_list(args):
    """Lista documentos. Args: [--status valid|expiring|expired] [--owner-type X] [limit]"""
    status     = None
    owner_type = None
    limit      = 30
    i = 0
    while i < len(args):
        if args[i] == "--status" and i + 1 < len(args):
            status = args[i+1]; i += 2
        elif args[i] == "--owner-type" and i + 1 < len(args):
            owner_type = args[i+1]; i += 2
        elif args[i].isdigit():
            limit = int(args[i]); i += 1
        else:
            i += 1
    filters  = ""
    params: dict = {"n": limit}
    if status:
        filters += " AND d.status = :status"; params["status"] = status
    if owner_type:
        filters += " AND d.owner_type = :ot"; params["ot"] = owner_type
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text(f"""
            SELECT d.id, d.owner_type, d.owner_id, d.doc_type, d.title,
                   d.issuer, d.expiry_date, d.status, d.sensitivity, d.source,
                   d.issue_date, d.notes, d.created_at
            FROM public.documents d
            WHERE 1=1 {filters}
            ORDER BY d.expiry_date ASC NULLS LAST, d.created_at DESC
            LIMIT :n
        """), params).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


def cmd_doc_expiring(args):
    """Documentos a expirar nos próximos N dias. Args: [days=30]"""
    days = int(args[0]) if args and args[0].isdigit() else 30
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT d.id, d.owner_type, d.owner_id, d.doc_type, d.title,
                   d.issuer, d.expiry_date, d.status, d.sensitivity,
                   d.expiry_date - CURRENT_DATE AS days_left
            FROM public.documents d
            WHERE d.expiry_date IS NOT NULL
              AND d.expiry_date >= CURRENT_DATE
              AND d.expiry_date <= CURRENT_DATE + (:days || ' days')::interval
            ORDER BY d.expiry_date ASC
        """), {"days": str(days)}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


def cmd_doc_requests(args):
    """Pedidos documentais abertos. Args: [limit]"""
    limit = int(args[0]) if args and args[0].isdigit() else 20
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT r.id, r.owner_type, r.owner_id, r.doc_type, r.status,
                   r.process_type, r.linked_case_id, r.due_date, r.notes,
                   r.requested_at,
                   r.due_date - CURRENT_DATE AS days_left
            FROM public.document_requests r
            WHERE r.status NOT IN ('done','failed')
            ORDER BY r.due_date ASC NULLS LAST, r.requested_at DESC
            LIMIT :n
        """), {"n": limit}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


# ── Vehicles ─────────────────────────────────────────────────────────────────

def cmd_vehicle_list(args):
    """Lista viaturas com estado dos documentos. Args: [limit]"""
    limit = int(args[0]) if args and args[0].isdigit() else 20
    engine, text = _conn()
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT v.id, v.matricula, v.marca, v.modelo, v.ano, v.cor,
                   v.estado, v.owner_type, v.owner_id, v.notes,
                   COUNT(d.id)                                                   AS total_docs,
                   COUNT(d.id) FILTER (WHERE d.status = 'expired')              AS docs_expired,
                   COUNT(d.id) FILTER (WHERE d.status = 'expiring')             AS docs_expiring,
                   COUNT(d.id) FILTER (WHERE d.status = 'valid')                AS docs_valid
            FROM public.vehicles v
            LEFT JOIN public.documents d ON d.owner_type = 'vehicle' AND d.owner_id = v.id
            WHERE v.estado != 'vendido'
            GROUP BY v.id
            ORDER BY docs_expired DESC, docs_expiring DESC, v.matricula
            LIMIT :n
        """), {"n": limit}).mappings().all()
    print(json.dumps([_row(r) for r in rows], ensure_ascii=False))


def cmd_vehicle_get(args):
    """Detalhe viatura + docs. Args: <id|matricula>"""
    if not args:
        print(json.dumps({"error": "id ou matricula obrigatório"})); return
    engine, text = _conn()
    lookup = args[0]
    param  = {"v": lookup}
    col    = "v.id = :v::int" if lookup.isdigit() else "v.matricula = :v"
    with engine.connect() as c:
        v = c.execute(text(f"""
            SELECT v.id, v.matricula, v.marca, v.modelo, v.ano, v.cor,
                   v.estado, v.owner_type, v.owner_id, v.notes, v.created_at
            FROM public.vehicles v WHERE {col}
        """), param).mappings().first()
        if not v:
            print(json.dumps({"error": "viatura não encontrada"})); return
        docs = c.execute(text("""
            SELECT id, doc_type, title, issuer, expiry_date, status, sensitivity, file_path
            FROM public.documents
            WHERE owner_type = 'vehicle' AND owner_id = :vid
            ORDER BY expiry_date ASC NULLS LAST
        """), {"vid": v["id"]}).mappings().all()
    print(json.dumps({
        "vehicle": _row(v),
        "documents": [_row(d) for d in docs],
    }, ensure_ascii=False))


CMDS = {
    "telemetry_history": cmd_telemetry_history,
    "telemetry_live":    cmd_telemetry_live,
    "workers":           cmd_workers,
    "worker_register":   cmd_worker_register,
    "worker_jobs":       cmd_worker_jobs,
    "worker_jobs_lease": cmd_worker_jobs_lease,
    "worker_jobs_report":cmd_worker_jobs_report,
    "events":            cmd_events,
    "backlog_recent":       cmd_backlog_recent,
    "syshealth":            cmd_syshealth,
    "worker_jobs_enqueue":  cmd_worker_jobs_enqueue,
    "worker_token_check":          cmd_worker_token_check,
    "twin_cases":                  cmd_twin_cases,
    "twin_cable_batch_create":     cmd_twin_cable_batch_create,
    "twin_batch_get":              cmd_twin_batch_get,
    "twin_batch_advance":          cmd_twin_batch_advance,
    "twin_batch_resultado":        cmd_twin_batch_resultado,
    "twin_batch_by_token":         cmd_twin_batch_by_token,
    "twin_batch_faturar":          cmd_twin_batch_faturar,
    "twin_batch_faturar_ok":       cmd_twin_batch_faturar_ok,
    "twin_factory_stats":          cmd_twin_factory_stats,
    "twin_tenders":                cmd_twin_tenders,
    "twin_tender_update":          cmd_twin_tender_update,
    "client_approve":              cmd_client_approve,
    "worker_tasks":                cmd_worker_tasks,
    "worker_tasks_history":        cmd_worker_tasks_history,
    "worker_task_start":           cmd_worker_task_start,
    "worker_task_done":            cmd_worker_task_done,
    "worker_task_skip":            cmd_worker_task_skip,
    "finance_invoice_create":      cmd_finance_invoice_create,
    "finance_invoice_list":        cmd_finance_invoice_list,
    "finance_invoice_pay":         cmd_finance_invoice_pay,
    "finance_stats":               cmd_finance_stats,
    "timesheet_start":             cmd_timesheet_start,
    "timesheet_stop":              cmd_timesheet_stop,
    "timesheet_list":              cmd_timesheet_list,
    "timesheet_approve":           cmd_timesheet_approve,
    "timesheet_all":               cmd_timesheet_all,
    "obligation_list":             cmd_obligation_list,
    "obligation_pay":              cmd_obligation_pay,
    "obligation_approve":          cmd_obligation_approve,
    "obligation_stats":            cmd_obligation_stats,
    "people_list":                 cmd_people_list,
    "client_list":                 cmd_client_list,
    "client_timesheets":           cmd_client_timesheets,
    "client_timesheet_action":     cmd_client_timesheet_action,
    "payout_list":                 cmd_payout_list,
    "payout_run":                  cmd_payout_run,
    "payout_mark_paid":            cmd_payout_mark_paid,
    "idea_create":                 cmd_idea_create,
    "idea_list":                   cmd_idea_list,
    "idea_get":                    cmd_idea_get,
    "idea_archive":                cmd_idea_archive,
    "idea_reviews":                cmd_idea_reviews,
    "idea_create_case":            cmd_idea_create_case,
    "decision_list":               cmd_decision_list,
    "decision_create":             cmd_decision_create,
    "decision_resolve":            cmd_decision_resolve,
    "invoice_generate":            cmd_invoice_generate,
    "invoice_push_toc":            cmd_invoice_push_toc,
    "invoice_drafts":              cmd_invoice_drafts,
    "toconline_status":            cmd_toconline_status,
    "timesheet_submit":            cmd_timesheet_submit,
    "agent_suggestions":           cmd_agent_suggestions,
    "agent_briefing":              cmd_agent_briefing,
    "agent_suggestion_read":       cmd_agent_suggestion_read,
    "bank_transactions":           cmd_bank_transactions,
    "bank_reconcile":              cmd_bank_reconcile,
    "bank_match":                  cmd_bank_match,
    "bank_ignore":                 cmd_bank_ignore,
    "incident_list":               cmd_incident_list,
    "incident_resolve":            cmd_incident_resolve,
    "incident_create":             cmd_incident_create,
    "cluster_metrics":             cmd_cluster_metrics,
    "agent_status":                cmd_agent_status,
    "pipeline_idea_analyze":       cmd_pipeline_idea_analyze,
    "pipeline_radar_score":        cmd_pipeline_radar_score,
    "pipeline_incidents":          cmd_pipeline_incidents,
    # Document Vault
    "doc_summary":                 cmd_doc_summary,
    "doc_list":                    cmd_doc_list,
    "doc_expiring":                cmd_doc_expiring,
    "doc_requests":                cmd_doc_requests,
    # Vehicles
    "vehicle_list":                cmd_vehicle_list,
    "vehicle_get":                 cmd_vehicle_get,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(f"usage: noc_query.py <{' | '.join(CMDS)}>", file=sys.stderr)
        sys.exit(1)
    try:
        CMDS[sys.argv[1]](sys.argv[2:])
    except Exception as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        sys.exit(1)
