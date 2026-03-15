#!/usr/bin/env python3
"""
pipeline_scheduler.py — AIOS Pipeline Scheduler
Runs every 10 minutes via systemd timer (aios-pipeline-scheduler.timer).

Enqueues cluster worker_jobs for:
  - incidents:  every tick   (~10 min)
  - radar:      every 3rd tick (~30 min) with cooldown check
  - ideas:      whenever open ideas have no pending analysis
"""
import sys, os, json, logging
from datetime import datetime, timezone

logging.basicConfig(
    format='%(asctime)s [pipeline_scheduler] %(levelname)s %(message)s',
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger(__name__)

_CLUSTER_ROOT = os.environ.get("AIOS_CLUSTER_ROOT", "/cluster/d1/ai-os")
DSN = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
# Strip sqlalchemy prefix if present (scheduler uses psycopg2 directly)
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


def has_active_job(conn, kind, cooldown_min, payload_json_contains=None):
    """True if a job of this kind is queued/running OR completed within cooldown_min."""
    q = """
        SELECT id FROM public.worker_jobs
        WHERE kind = %s
          AND (
                status IN ('queued', 'running')
                OR (status = 'done' AND ts_done > NOW() - (%s || ' minutes')::interval)
              )
    """
    params = [kind, str(cooldown_min)]
    if payload_json_contains:
        q += " AND payload @> %s::jsonb"
        params.append(json.dumps(payload_json_contains))
    q += " LIMIT 1"
    with conn.cursor() as cur:
        cur.execute(q, params)
        return cur.fetchone() is not None


# ── pipeline 3: incidents ─────────────────────────────────────────────────────

def schedule_incidents(conn):
    incidents_payload = {"cmd": f"python3 {_CLUSTER_ROOT}/bin/incidents_tick.py"}
    if has_active_job(conn, "automation", 8, incidents_payload):
        log.info("incidents: skip — job active/recent")
        return None
    jid = enqueue(conn, "automation", incidents_payload)
    log.info(f"incidents: enqueued → job_id={jid}")
    return jid


# ── pipeline 2: radar ─────────────────────────────────────────────────────────

def schedule_radar(conn):
    if has_active_job(conn, "radar", 28):
        log.info("radar: skip — job active/recent (<28min)")
        return None
    jid = enqueue(conn, "radar", {"script": "radar_score", "args": ["--source", "ted"]})
    log.info(f"radar: enqueued → job_id={jid}")
    return jid


# ── pipeline 1: idea analysis ─────────────────────────────────────────────────

def schedule_ideas(conn):
    """Enqueue analysis for open ideas that have a message but no active analysis job."""
    with conn.cursor(cursor_factory=__import__('psycopg2').extras.RealDictCursor) as cur:
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
        open_ideas = cur.fetchall()

    if not open_ideas:
        log.info("ideas: skip — no open ideas awaiting analysis")
        return []

    enqueued = []
    for row in open_ideas:
        tid = row["id"]
        jid = enqueue(conn, "ai_analysis", {"thread_id": tid})
        log.info(f"ideas: enqueued analysis for thread_id={tid} → job_id={jid}")
        enqueued.append({"thread_id": tid, "job_id": jid})
    return enqueued


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== pipeline_scheduler tick ===")
    conn = _conn()
    try:
        results = {
            "incidents": schedule_incidents(conn),
            "radar":     schedule_radar(conn),
            "ideas":     schedule_ideas(conn),
        }
        conn.commit()
        log.info(f"tick done: {json.dumps(results)}")
        print(json.dumps(results))
    except Exception as e:
        conn.rollback()
        log.error(f"scheduler error: {e}")
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
