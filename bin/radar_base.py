#!/usr/bin/env python3
"""
radar_base.py — BASE.gov.pt collector
Source: dados.gov.pt public JSON (biweekly, no auth required)

Fetches anuncios2026.json, filters recent entries, stores raw in radar_raw_items.
Then calls normalize → score → bridge pipeline.

Usage:
    python3 bin/radar_base.py [--days N] [--dry-run]
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, hashlib, json, time
from datetime import date, datetime, timedelta, timezone
from urllib.request import urlopen, Request
from urllib.error import URLError

import sqlalchemy as sa

DB_URL = _os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
engine = sa.create_engine(DB_URL)

DATASET_API = "https://dados.gov.pt/api/1/datasets/contratos-publicos-portal-base-impic-anuncios-de-2012-a-2026/"
UA = "AIOS-Radar/1.0 (ai-os; national procurement radar)"
DEFAULT_DAYS = 14   # dados.gov.pt updates biweekly; 14d catches all new items

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _get_json(url: str, timeout: int = 30) -> dict | list:
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))

# ── Get current year JSON URL from dados.gov.pt ───────────────────────────────

def _get_json_url() -> str:
    """Retrieve the URL of the current year anuncios JSON from dados.gov.pt."""
    meta = _get_json(DATASET_API)
    year = date.today().year
    resources = meta.get("resources") or []
    # Prefer current year JSON
    for res in resources:
        title = (res.get("title") or "").lower()
        mime  = (res.get("mime") or res.get("format") or "").lower()
        if str(year) in title and ("json" in mime or title.endswith(".json")):
            return res["url"]
    # Fallback: any JSON resource
    for res in resources:
        mime = (res.get("mime") or res.get("format") or "").lower()
        if "json" in mime:
            return res["url"]
    raise RuntimeError("No JSON resource found in dados.gov.pt dataset")

# ── Date parsing for BASE anuncios ────────────────────────────────────────────

def _parse_pub_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None

# ── Create radar_runs entry ───────────────────────────────────────────────────

def _start_run() -> int:
    with engine.begin() as c:
        return c.execute(sa.text("""
            INSERT INTO public.radar_runs (source, status)
            VALUES ('base', 'running')
            RETURNING id
        """)).scalar()

def _finish_run(run_id: int, raw_count: int, status: str = "ok", error: str | None = None):
    with engine.begin() as c:
        c.execute(sa.text("""
            UPDATE public.radar_runs
            SET finished_at=NOW(), raw_count=:rc, status=:st, error_log=:err
            WHERE id=:id
        """), {"rc": raw_count, "st": status, "err": error, "id": run_id})

# ── Main ──────────────────────────────────────────────────────────────────────

def run(days: int = DEFAULT_DAYS, dry_run: bool = False) -> dict:
    run_id = _start_run()
    cutoff = date.today() - timedelta(days=days)

    print(f"[radar_base] run_id={run_id} cutoff={cutoff} dry_run={dry_run}")

    try:
        json_url = _get_json_url()
        print(f"[radar_base] fetching {json_url}")
        data = _get_json(json_url, timeout=60)
    except Exception as e:
        _finish_run(run_id, 0, "error", str(e))
        print(json.dumps({"error": str(e), "run_id": run_id}))
        return {"error": str(e)}

    if not isinstance(data, list):
        # Some responses are {"data": [...]}
        data = data.get("data") or data.get("anuncios") or []

    stored = 0
    skipped = 0
    recent = 0

    for item in data:
        if not isinstance(item, dict):
            continue

        pub_date = _parse_pub_date(
            item.get("publicacao") or item.get("dataPublicacao") or item.get("data")
        )

        # Filter by date
        if pub_date and pub_date < cutoff:
            continue
        recent += 1

        # Use nAnuncio as external_id (IdIncm can be -1 for JORAA entries)
        eid = str(item.get("nAnuncio") or item.get("IdIncm") or "")
        if not eid or eid == "-1":
            skipped += 1
            continue

        payload_str = json.dumps(item, ensure_ascii=False, sort_keys=True)
        payload_hash = hashlib.md5(payload_str.encode()).hexdigest()

        if dry_run:
            print(f"  DRY  base/{eid}  pub={pub_date}")
            stored += 1
            continue

        with engine.begin() as c:
            result = c.execute(sa.text("""
                INSERT INTO public.radar_raw_items (source, external_id, payload, hash, run_id)
                VALUES ('base', :eid, :payload, :hash, :run_id)
                ON CONFLICT (source, external_id) DO NOTHING
                RETURNING id
            """), {"eid": eid, "payload": payload_str, "hash": payload_hash, "run_id": run_id})
            if result.rowcount > 0:
                stored += 1
            else:
                skipped += 1

    _finish_run(run_id, stored)

    print(json.dumps({
        "run_id": run_id, "total_in_file": len(data),
        "recent": recent, "stored": stored, "skipped": skipped,
    }))
    return {"run_id": run_id, "stored": stored}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help="Fetch announcements published in last N days (default 3)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.days, args.dry_run)
