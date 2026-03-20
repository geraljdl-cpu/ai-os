#!/usr/bin/env python3
# Remover bin/ do sys.path para evitar shadowing do stdlib
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

"""
autonomia_orchestrator.py — Gestão automática do ciclo de vida de worker_jobs.

Corre via systemd timer (a cada 60s).
Responsabilidades:
  1. retry_failed_jobs  — jobs failed + retry_count < max_retries → queued (backoff 1h)
  2. detect_zombies     — jobs running > ZOMBIE_THRESHOLD min → failed (timeout watchdog)
  3. collect_stats      — métricas para logging/journal
"""

import json
import logging
import os

# ── Load env ──────────────────────────────────────────────────────────────────
_env_file = "/etc/aios.env"
if os.path.exists(_env_file):
    for _line in open(_env_file):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ[_k.strip()] = _v.strip()

# ── Config ────────────────────────────────────────────────────────────────────
DATABASE_URL     = os.environ.get(
    "DATABASE_URL",
    "postgresql://aios_user:jdl@127.0.0.1:5432/aios"
)
ZOMBIE_THRESHOLD = 15   # minutos até job running ser declarado zombie
RETRY_WINDOW     = 60   # minutos — só retries de falhas recentes

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [autonomia] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S"
)
log = logging.getLogger(__name__)


def _conn():
    import sqlalchemy as sa
    engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)
    return engine, sa.text


def retry_failed_jobs(engine, text) -> list:
    """
    Jobs failed com retry_count < max_retries e falha recente → queued.
    Incrementa retry_count. Devolve lista de ids retentados.
    """
    with engine.begin() as conn:
        rows = conn.execute(text("""
            UPDATE public.worker_jobs
            SET status      = 'queued',
                retry_count = retry_count + 1,
                ts_assigned = NULL,
                ts_done     = NULL
            WHERE status = 'failed'
              AND retry_count < max_retries
              AND ts_done > NOW() - INTERVAL ':window minutes'
            RETURNING id, kind, retry_count, max_retries
        """.replace(':window', str(RETRY_WINDOW)))).mappings().all()
    retried = [dict(r) for r in rows]
    for r in retried:
        log.info(f"Retry job #{r['id']} ({r['kind']}) — tentativa {r['retry_count']}/{r['max_retries']}")
    return retried


def detect_zombies(engine, text) -> list:
    """
    Jobs running há mais de ZOMBIE_THRESHOLD minutos → failed (timeout watchdog).
    Devolve lista de ids marcados como zombie.
    """
    zombie_result = '{"error":"timeout watchdog","zombie":true}'
    with engine.begin() as conn:
        rows = conn.execute(text("""
            UPDATE public.worker_jobs
            SET status  = 'failed',
                result  = :zresult,
                ts_done = NOW()
            WHERE status = 'running'
              AND ts_assigned < NOW() - INTERVAL ':threshold minutes'
            RETURNING id, kind, assigned_worker_id
        """.replace(':threshold', str(ZOMBIE_THRESHOLD))), {"zresult": zombie_result}).mappings().all()
    zombies = [dict(r) for r in rows]
    for z in zombies:
        log.warning(f"Zombie job #{z['id']} ({z['kind']}) worker={z['assigned_worker_id']} → failed")
    return zombies


def collect_stats(engine, text) -> dict:
    """Métricas de estado dos jobs (últimas 24h) + blocked_review pendentes."""
    with engine.connect() as conn:
        counts = conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE status = 'queued')                           AS queued,
                COUNT(*) FILTER (WHERE status = 'running')                          AS running,
                COUNT(*) FILTER (WHERE status = 'done'
                                   AND ts_done > NOW() - INTERVAL '24h')            AS done_24h,
                COUNT(*) FILTER (WHERE status = 'failed'
                                   AND ts_done > NOW() - INTERVAL '24h')            AS failed_24h,
                COUNT(*) FILTER (WHERE status = 'blocked_review')                   AS blocked_review
            FROM public.worker_jobs
        """)).mappings().one()
    return {
        "queued":         int(counts["queued"]        or 0),
        "running":        int(counts["running"]       or 0),
        "done_24h":       int(counts["done_24h"]      or 0),
        "failed_24h":     int(counts["failed_24h"]    or 0),
        "blocked_review": int(counts["blocked_review"] or 0),
    }


def run() -> dict:
    engine, text = _conn()

    zombies = detect_zombies(engine, text)
    retried = retry_failed_jobs(engine, text)
    stats   = collect_stats(engine, text)

    result = {
        "zombies_detected": len(zombies),
        "retries_queued":   len(retried),
        "stats":            stats,
    }
    log.info(f"Orchestrator concluído: {result}")
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result))
