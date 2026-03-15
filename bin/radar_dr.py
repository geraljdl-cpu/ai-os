#!/usr/bin/env python3
"""
radar_dr.py — Diário da República collector
Source: RSS Série II, Parte L (Procedimentos de Contratação Pública)
URL: https://files.diariodarepublica.pt/rss/serie2&parte=l-html.xml?data=YYYY-MM-DD

Fetches the last N days of procurement announcements (anúncios de procedimento),
stores raw in radar_raw_items. Then normalize → score → bridge pipeline runs.

Usage:
    python3 bin/radar_dr.py [--days N] [--dry-run]
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, hashlib, json, re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

import sqlalchemy as sa

DB_URL = _os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
engine = sa.create_engine(DB_URL)

RSS_BASE   = "https://files.diariodarepublica.pt/rss/serie2&parte=l-html.xml"
UA         = "AIOS-Radar/1.0 (ai-os; national procurement radar)"
DEFAULT_DAYS = 7

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _fetch_rss(target_date: date, timeout: int = 20) -> list[dict]:
    """Fetch RSS for a specific date, return list of raw item dicts."""
    url = f"{RSS_BASE}?data={target_date.isoformat()}"
    req = Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml,*/*"})
    try:
        with urlopen(req, timeout=timeout) as r:
            xml_bytes = r.read()
    except HTTPError as e:
        if e.code == 404:
            return []   # no gazette that day (weekend/holiday)
        raise
    except URLError:
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []

    items = []
    for el in root.findall("./channel/item"):
        title  = (el.findtext("title") or "").strip()
        desc   = re.sub(r"\s+", " ", (el.findtext("description") or "").strip())
        link   = (el.findtext("link") or "").strip()

        # Extract anúncio number: "n.º 6231/2026" → "6231/2026"
        m = re.search(r"n\.º\s+([\d]+/\d{4})", title)
        if not m:
            continue
        anuncio_id = m.group(1)

        # Extract publication date from title: "Série II de 2026-03-13"
        dm = re.search(r"(\d{4}-\d{2}-\d{2})", title)
        pub_date_str = dm.group(1) if dm else target_date.isoformat()

        # Best-effort entity extraction:
        # description = "Entity Name   Procedure description"
        # Split on first occurrence of a verb/noun that starts a procedure type
        _PROC_STARTERS = re.compile(
            r"\b(Aquisição|Aquisi|Empreitada|Empreit|Prestação|Prest|Alienação|Alien|"
            r"Concurso|Fornecimento|Forneç|Contratação|Contr|Locação|Gestão|Servi|"
            r"Manutenção|Requalificação|Reabil|Constru|Obras?|Adquir|Compra)\b",
            re.IGNORECASE
        )
        entity = ""
        description_text = desc
        pm = _PROC_STARTERS.search(desc)
        if pm and pm.start() > 3:
            entity = desc[:pm.start()].strip()
            description_text = desc[pm.start():].strip()

        items.append({
            "id":       anuncio_id,
            "titulo":   description_text or desc,
            "entidade": entity,
            "sumario":  desc,
            "url":      link,
            "data":     pub_date_str,
            "prazo":    None,
        })

    return items

# ── DB helpers ─────────────────────────────────────────────────────────────────

def _start_run() -> int:
    with engine.begin() as c:
        return c.execute(sa.text("""
            INSERT INTO public.radar_runs (source, status)
            VALUES ('dr', 'running')
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
    print(f"[radar_dr] run_id={run_id} days={days} dry_run={dry_run}")

    today   = date.today()
    stored  = 0
    skipped = 0
    total   = 0

    try:
        for delta in range(days):
            target = today - timedelta(days=delta)
            items  = _fetch_rss(target)
            if not items:
                continue
            print(f"[radar_dr]   {target.isoformat()}: {len(items)} items")
            total += len(items)

            for item in items:
                eid          = item["id"]
                payload_str  = json.dumps(item, ensure_ascii=False, sort_keys=True)
                payload_hash = hashlib.md5(payload_str.encode()).hexdigest()

                if dry_run:
                    print(f"  DRY  dr/{eid}  date={item['data']}")
                    stored += 1
                    continue

                with engine.begin() as c:
                    result = c.execute(sa.text("""
                        INSERT INTO public.radar_raw_items
                            (source, external_id, payload, hash, run_id)
                        VALUES ('dr', :eid, :payload, :hash, :run_id)
                        ON CONFLICT (source, external_id) DO NOTHING
                        RETURNING id
                    """), {"eid": eid, "payload": payload_str,
                           "hash": payload_hash, "run_id": run_id})
                    if result.rowcount > 0:
                        stored += 1
                    else:
                        skipped += 1

    except Exception as e:
        _finish_run(run_id, stored, "error", str(e))
        print(json.dumps({"error": str(e), "run_id": run_id}))
        return {"error": str(e)}

    _finish_run(run_id, stored)
    result = {"run_id": run_id, "days_fetched": days,
              "total_parsed": total, "stored": stored, "skipped": skipped}
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(args.days, args.dry_run)
