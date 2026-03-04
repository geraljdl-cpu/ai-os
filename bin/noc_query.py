#!/usr/bin/env python3
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
import subprocess
import sys
import time
from datetime import datetime, timezone

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def _row(r) -> dict:
    d = dict(r)
    # converte datetime para ISO string
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
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
            SELECT id, title, goal, status, priority, task_type,
                   attempts, last_error, created_at, updated_at
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


# ── dispatch ──────────────────────────────────────────────────────────────────

CMDS = {
    "telemetry_history": cmd_telemetry_history,
    "telemetry_live":    cmd_telemetry_live,
    "workers":           cmd_workers,
    "worker_register":   cmd_worker_register,
    "worker_jobs":       cmd_worker_jobs,
    "worker_jobs_lease": cmd_worker_jobs_lease,
    "worker_jobs_report":cmd_worker_jobs_report,
    "events":            cmd_events,
    "backlog_recent":    cmd_backlog_recent,
    "syshealth":         cmd_syshealth,
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
