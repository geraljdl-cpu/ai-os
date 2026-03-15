#!/usr/bin/env python3
"""
radar_dr.py — Diário da República collector (STUB)

Status: Phase 2 — diariodarepublica.pt API requires session auth.
This stub exists so the pipeline and timers are in place.

When an accessible endpoint is confirmed (RSS, public API, or auth token),
implement _fetch_dr() and remove the stub exit.

Usage:
    python3 bin/radar_dr.py [--dry-run]
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import json
import sqlalchemy as sa

DB_URL = _os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
engine = sa.create_engine(DB_URL)

# ── Phase 2 placeholder ───────────────────────────────────────────────────────
# Candidate endpoints to try when available:
#   https://diariodarepublica.pt/rss/concursos.xml
#   https://diariodarepublica.pt/api/v2/search?type=anuncio_procedimento
#   OCDS export from dados.gov.pt (if made available for DR)

def run(dry_run: bool = False) -> dict:
    with engine.begin() as c:
        run_id = c.execute(sa.text("""
            INSERT INTO public.radar_runs (source, status, finished_at, error_log)
            VALUES ('dr', 'ok', NOW(), 'STUB: DR API not yet available (Phase 2)')
            RETURNING id
        """)).scalar()

    msg = "DR collector: stub — API pendente para Fase 2"
    print(json.dumps({"run_id": run_id, "status": "stub", "message": msg}))
    return {"stub": True, "run_id": run_id}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.dry_run)
