#!/usr/bin/env python3
"""
radar_ted.py — TED collector (fetch → radar_raw_items)
Part of the 5-module pipeline: collect → normalize → score → bridge

Usage:
    python3 bin/radar_ted.py              # fetch + store raw
    python3 bin/radar_ted.py --dry-run    # fetch only, no DB write
    python3 bin/radar_ted.py --list [N]   # list stored tenders from twin
    python3 bin/radar_ted.py --backfill   # add source/external_id to legacy TED entities

Scheduled: daily 07:00 via aios-radar.timer
Full pipeline: bash bin/radar_run_all.sh --source ted
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import hashlib, json, os, sys, time
from pathlib import Path

import requests
import sqlalchemy as sa

# ── Config ────────────────────────────────────────────────────────────────────

AIOS_ROOT = Path(os.environ.get("AIOS_ROOT", Path.home() / "ai-os"))
ENV_DB    = Path(os.environ.get("AIOS_ENV_DB", Path.home() / ".env.db"))

def _load_env():
    for p in [ENV_DB, Path("/etc/aios.env")]:
        if p.exists():
            for line in p.read_text().splitlines():
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
TG_TOKEN     = os.environ.get("AIOS_TG_TOKEN", "").strip()
TG_CHAT      = os.environ.get("AIOS_TG_CHAT", "").strip()
UI_BASE      = os.environ.get("AIOS_UI_BASE", "http://127.0.0.1:3000").rstrip("/")

TED_API         = "https://api.ted.europa.eu/v3/notices/search"
SCORE_THRESHOLD = 15

engine = sa.create_engine(DATABASE_URL, pool_pre_ping=True, future=True)

# ── Keyword groups ────────────────────────────────────────────────────────────

KEYWORD_GROUPS = [
    {
        "name":  "reciclagem_cabos",
        "query": "organisation-country-buyer = PRT AND description-proc ~ reciclagem",
    },
    {
        "name":  "gestao_residuos",
        "query": "organisation-country-buyer = PRT AND description-proc ~ residuos",
    },
    {
        "name":  "manutencao_eletrica",
        "query": "organisation-country-buyer = PRT AND description-proc ~ electrica",
    },
    {
        "name":  "obras_industriais",
        "query": "organisation-country-buyer = PRT AND description-proc ~ industrial",
    },
]

FIELDS = [
    "notice-type", "contract-nature", "deadline-receipt-tender-date-lot",
    "framework-maximum-value-lot", "description-glo",
]

# ── TED fetch ─────────────────────────────────────────────────────────────────

def _ted_search(query: str, limit: int = 50) -> list[dict]:
    payload = {
        "query": query, "fields": FIELDS, "limit": limit,
        "scope": "ALL", "paginationMode": "ITERATION",
    }
    try:
        r = requests.post(TED_API, json=payload, timeout=30)
        r.raise_for_status()
        return r.json().get("notices", [])
    except Exception as e:
        print(f"[radar_ted] TED fetch error: {e}", file=sys.stderr)
        return []

# ── radar_runs entry ──────────────────────────────────────────────────────────

def _start_run() -> int:
    with engine.begin() as c:
        return c.execute(sa.text(
            "INSERT INTO public.radar_runs (source, status) VALUES ('ted','running') RETURNING id"
        )).scalar()

def _finish_run(run_id: int, raw_count: int, status: str = "ok", error: str | None = None):
    with engine.begin() as c:
        c.execute(sa.text(
            "UPDATE public.radar_runs SET finished_at=NOW(), raw_count=:rc, status=:st, error_log=:err WHERE id=:id"
        ), {"rc": raw_count, "st": status, "err": error, "id": run_id})

# ── Telegram alert ────────────────────────────────────────────────────────────

def _tg_alert(pub_num: str, title: str, score: int, pdf_url: str = ""):
    if not TG_TOKEN or not TG_CHAT:
        return
    link = f"\n  PDF: {pdf_url}" if pdf_url else ""
    msg  = (
        f"📡 Radar TED — novo concurso\n"
        f"  Score: {score}\n"
        f"  {title[:80]}\n"
        f"  Ref: {pub_num}{link}\n"
        f"  Ver: {UI_BASE}/tenders"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "disable_web_page_preview": True},
            timeout=20,
        )
    except Exception as e:
        print(f"[radar_ted] Telegram error: {e}", file=sys.stderr)

# ── List stored tenders ───────────────────────────────────────────────────────

def _list_tenders(limit: int = 30):
    with engine.connect() as c:
        rows = c.execute(sa.text("""
            SELECT e.id, e.name, e.metadata, e.created_at
            FROM public.twin_entities e
            WHERE e.type = 'tender'
            ORDER BY (e.metadata->>'score')::int DESC, e.created_at DESC
            LIMIT :n
        """), {"n": limit}).mappings().all()
    out = []
    for r in rows:
        meta = r["metadata"] if isinstance(r["metadata"], dict) else json.loads(r["metadata"] or "{}")
        out.append({
            "entity_id": r["id"],
            "title":     meta.get("title", r["name"]),
            "pub_num":   meta.get("pub_num", meta.get("external_id", "")),
            "score":     meta.get("score", 0),
            "estado":    meta.get("estado", "novo"),
            "deadline":  meta.get("deadline", ""),
            "nature":    meta.get("nature", ""),
            "grupo":     meta.get("grupo", ""),
            "pdf_url":   meta.get("pdf_url", ""),
            "source":    meta.get("source", "ted"),
            "created_at": r["created_at"].isoformat() if r["created_at"] else "",
        })
    return out

# ── Backfill legacy TED entities ──────────────────────────────────────────────

def _backfill():
    """Add source='ted' + external_id=pub_num to legacy TED entities."""
    with engine.connect() as c:
        rows = c.execute(sa.text("""
            SELECT id, metadata FROM public.twin_entities
            WHERE type = 'tender'
              AND (metadata->>'source' IS NULL OR metadata->>'source' = '')
        """)).mappings().all()

    updated = 0
    for row in rows:
        meta = row["metadata"] if isinstance(row["metadata"], dict) else json.loads(row["metadata"] or "{}")
        pub_num = meta.get("pub_num") or meta.get("external_id") or ""
        if not pub_num:
            continue
        meta["source"]      = "ted"
        meta["external_id"] = pub_num
        with engine.begin() as c:
            c.execute(sa.text(
                "UPDATE public.twin_entities SET metadata=:m WHERE id=:id"
            ), {"m": json.dumps(meta), "id": row["id"]})
        updated += 1

    print(json.dumps({"backfilled": updated}))

# ── Main collector ────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> dict:
    run_id  = _start_run() if not dry_run else None
    seen    = set()
    stored  = 0
    skipped = 0
    alerted = 0

    for grp in KEYWORD_GROUPS:
        print(f"[radar_ted] grupo={grp['name']} ...", file=sys.stderr)
        notices = _ted_search(grp["query"], limit=50)

        for notice in notices:
            pub_num = notice.get("publication-number", "")
            if not pub_num or pub_num in seen:
                continue
            seen.add(pub_num)

            # Attach group hint to payload for normalizer
            notice["_grupo_hint"] = grp["name"]

            payload_str  = json.dumps(notice, ensure_ascii=False, sort_keys=True)
            payload_hash = hashlib.md5(payload_str.encode()).hexdigest()

            if dry_run:
                print(f"  DRY ted/{pub_num}")
                stored += 1
                continue

            with engine.begin() as c:
                result = c.execute(sa.text("""
                    INSERT INTO public.radar_raw_items (source, external_id, payload, hash, run_id)
                    VALUES ('ted', :eid, :payload, :hash, :run_id)
                    ON CONFLICT (source, external_id) DO NOTHING
                    RETURNING id
                """), {"eid": pub_num, "payload": payload_str, "hash": payload_hash, "run_id": run_id})
                is_new = result.rowcount > 0

            if is_new:
                stored += 1
                # Quick score for TG alert (no full pipeline yet)
                from radar_score import _score, SCORE_THRESHOLD as ST
                norm_stub = {
                    "external_id": pub_num,
                    "title": notice.get("publication-number",""),
                    "description": str(notice.get("description-glo","")),
                    "cpv": None, "deadline": None, "base_value": None,
                }
                score, _, _ = _score(norm_stub)
                if score >= ST:
                    _tg_alert(pub_num, pub_num, score)
                    alerted += 1
                    time.sleep(0.5)
            else:
                skipped += 1

        time.sleep(1)

    if run_id:
        _finish_run(run_id, stored)

    result = {"run_id": run_id, "stored": stored, "skipped": skipped, "alerted": alerted}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    args = sys.argv[1:]

    if "--backfill" in args:
        _backfill()
        sys.exit(0)

    if "--list" in args:
        limit = next((int(a) for a in args if a.isdigit()), 30)
        print(json.dumps(_list_tenders(limit), ensure_ascii=False))
        sys.exit(0)

    dry = "--dry-run" in args
    run(dry_run=dry)
