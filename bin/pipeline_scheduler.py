#!/usr/bin/env python3
"""
pipeline_scheduler.py — AIOS Pipeline Scheduler
Runs every 1 minute via systemd timer (aios-pipeline-scheduler.timer).

Pipelines (auto-managed cooldowns):
  ideas:      every ~1 min  — ideias abertas sem análise → ai_analysis
  incidents:  every ~10 min — detecção de problemas     → automation
  radar:      every ~30 min — scoring de concursos      → radar
  finance:    every ~1 h    — obrigações e pagamentos   → automation
  obligations:on-demand    — obrigações vencidas        → automation
  cases:      every ~2 h    — cases parados/bloqueados  → general
  briefing:   08:25–09:00   — resumo diário de manhã    → general
  closing:    17:25–18:00   — fecho do dia              → general
"""
import sys, os, json, logging
from datetime import datetime, timezone

logging.basicConfig(
    format='%(asctime)s [scheduler] %(levelname)s %(message)s',
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_CLUSTER_ROOT = os.environ.get("AIOS_CLUSTER_ROOT", "/cluster/d1/ai-os")
_LOCAL_ROOT   = os.environ.get("AIOS_ROOT", "/home/jdl/ai-os")
DSN = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
if DSN.startswith("postgresql+"):
    DSN = "postgresql" + DSN[DSN.index("://"):]


def _conn():
    import psycopg2, psycopg2.extras
    return psycopg2.connect(DSN)


def enqueue(conn, kind, payload):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.worker_jobs (ts_created, status, kind, payload) "
            "VALUES (NOW(), 'queued', %s, %s) RETURNING id",
            (kind, json.dumps(payload))
        )
        return cur.fetchone()[0]


def has_active_job(conn, kind, cooldown_min, payload_contains=None):
    """True if job is queued/running OR completed within cooldown_min minutes."""
    q = """
        SELECT id FROM public.worker_jobs
        WHERE kind = %s
          AND (
                status IN ('queued', 'running')
                OR (status = 'done' AND ts_done > NOW() - (%s || ' minutes')::interval)
              )
    """
    params = [kind, str(cooldown_min)]
    if payload_contains:
        q += " AND payload @> %s::jsonb"
        params.append(json.dumps(payload_contains))
    q += " LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchone() is not None


# ── Rule 1: idea analysis — cada 1min ────────────────────────────────────────

def schedule_ideas(conn):
    """Ideias abertas com mensagem e sem job activo → enqueue ai_analysis."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT t.id
            FROM public.idea_threads t
            WHERE t.status = 'open'
              AND EXISTS (
                    SELECT 1 FROM public.idea_messages m
                    WHERE m.thread_id = t.id AND m.role = 'joao'
                  )
              AND NOT EXISTS (
                    SELECT 1 FROM public.worker_jobs wj
                    WHERE wj.kind = 'ai_analysis'
                      AND wj.payload->>'thread_id' = t.id::text
                      AND wj.status IN ('queued', 'running')
                  )
        """)
        ideas = cur.fetchall()

    if not ideas:
        return []
    enqueued = []
    for row in ideas:
        tid = row["id"]
        jid = enqueue(conn, "ai_analysis", {"thread_id": tid})
        log.info(f"ideas: thread_id={tid} → job_id={jid}")
        enqueued.append({"thread_id": tid, "job_id": jid})
    return enqueued


# ── Rule 2: incidents — cada 10min ───────────────────────────────────────────

def schedule_incidents(conn):
    payload = {"cmd": f"python3 {_CLUSTER_ROOT}/bin/incidents_tick.py"}
    if has_active_job(conn, "automation", 8, payload):
        return None
    jid = enqueue(conn, "automation", payload)
    log.info(f"incidents: job_id={jid}")
    return jid


# ── Rule 3: radar — cada 30min ───────────────────────────────────────────────

def schedule_radar(conn):
    if has_active_job(conn, "radar", 28):
        return None
    jid = enqueue(conn, "radar", {"script": "radar_score", "args": ["--source", "ted"]})
    log.info(f"radar: job_id={jid}")
    return jid


# ── Rule 4: finance — cada 1h ────────────────────────────────────────────────

def schedule_finance(conn):
    payload = {"cmd": f"python3 {_CLUSTER_ROOT}/bin/finance_tick.py"}
    if has_active_job(conn, "automation", 55, payload):
        return None
    jid = enqueue(conn, "automation", payload)
    log.info(f"finance: job_id={jid}")
    return jid


# ── Rule 5: obligations trigger — cada tick (se vencidas) ────────────────────

def schedule_obligation_alert(conn):
    """Se há obrigações vencidas e ainda não há job de finance recente, força tick."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COUNT(*) AS n FROM public.finance_obligations
            WHERE status = 'pending' AND due_date < CURRENT_DATE
        """)
        n = cur.fetchone()["n"]
    if n == 0:
        return None
    payload = {"cmd": f"python3 {_CLUSTER_ROOT}/bin/finance_tick.py"}
    if has_active_job(conn, "automation", 25, payload):
        return None
    jid = enqueue(conn, "automation", payload)
    log.info(f"obligation_alert: {n} vencidas → job_id={jid}")
    return jid


# ── Rule 6: cases parados — cada 2h ──────────────────────────────────────────

def schedule_stale_cases(conn):
    """Cases em estado aberto sem actividade há >24h → enqueue general."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT COUNT(*) AS n FROM public.twin_cases
            WHERE status NOT IN ('closed', 'archived', 'done')
              AND updated_at < NOW() - INTERVAL '24 hours'
        """)
        n = cur.fetchone()["n"]
    if n == 0:
        return None
    payload = {"cmd": f"python3 {_CLUSTER_ROOT}/bin/incidents_tick.py"}
    if has_active_job(conn, "general", 120, payload):
        return None
    jid = enqueue(conn, "general", payload)
    log.info(f"stale_cases: {n} casos parados → job_id={jid}")
    return jid


# ── Rule 7: briefing diário 08:30 ────────────────────────────────────────────

def schedule_briefing(conn):
    """Janela 08:25–09:05 UTC+0. Cooldown 8h para não repetir no mesmo dia."""
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    if not ((h == 8 and m >= 25) or (h == 9 and m <= 5)):
        return None
    payload = {"cmd": f"python3 {_CLUSTER_ROOT}/bin/joao_agent.py morning"}
    if has_active_job(conn, "general", 480, payload):
        return None
    jid = enqueue(conn, "general", payload)
    log.info(f"briefing morning: job_id={jid}")
    return jid


# ── Rule 8: fecho diário 17:30 ───────────────────────────────────────────────

def schedule_closing(conn):
    """Janela 17:25–18:05 UTC+0. Cooldown 8h para não repetir."""
    now = datetime.now(timezone.utc)
    h, m = now.hour, now.minute
    if not ((h == 17 and m >= 25) or (h == 18 and m <= 5)):
        return None
    payload = {"cmd": f"python3 {_CLUSTER_ROOT}/bin/joao_agent.py evening"}
    if has_active_job(conn, "general", 480, payload):
        return None
    jid = enqueue(conn, "general", payload)
    log.info(f"closing evening: job_id={jid}")
    return jid


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== pipeline_scheduler tick ===")
    conn = _conn()
    try:
        results = {
            "ideas":             schedule_ideas(conn),
            "incidents":         schedule_incidents(conn),
            "radar":             schedule_radar(conn),
            "finance":           schedule_finance(conn),
            "obligation_alert":  schedule_obligation_alert(conn),
            "stale_cases":       schedule_stale_cases(conn),
            "briefing":          schedule_briefing(conn),
            "closing":           schedule_closing(conn),
        }
        conn.commit()
        # log apenas resultados não-nulos
        active = {k: v for k, v in results.items() if v not in (None, [])}
        if active:
            log.info(f"enqueued: {json.dumps(active)}")
        else:
            log.info("tick: nothing to enqueue")
        print(json.dumps(results))
    except Exception as e:
        conn.rollback()
        log.error(f"scheduler error: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
