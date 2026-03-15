#!/usr/bin/env python3
"""
radar_normalize.py — Normalizer module
Reads radar_raw_items for a given source and writes to radar_normalized.

Usage:
    python3 bin/radar_normalize.py --source base [--run-id N]
    python3 bin/radar_normalize.py --source ted  [--run-id N]
    python3 bin/radar_normalize.py --source dr   [--run-id N]
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, hashlib, json, re
from datetime import date, datetime

import sqlalchemy as sa

DB_URL = _os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
engine = sa.create_engine(DB_URL)

# ── date parsing helpers ──────────────────────────────────────────────────────

_PT_MONTHS = {
    "jan":1,"fev":2,"mar":3,"abr":4,"mai":5,"jun":6,
    "jul":7,"ago":8,"set":9,"out":10,"nov":11,"dez":12,
}

def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    s = str(s).strip()
    # ISO
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        try: return date(int(m[1]), int(m[2]), int(m[3]))
        except: pass
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", s)
    if m:
        try: return date(int(m[3]), int(m[2]), int(m[1]))
        except: pass
    # "15 Mar 2026"
    m = re.match(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", s, re.I)
    if m:
        mo = _PT_MONTHS.get(m[2].lower()[:3])
        if mo:
            try: return date(int(m[3]), mo, int(m[1]))
            except: pass
    return None

def _clean(s) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()

# ── BASE normalizer ───────────────────────────────────────────────────────────

def _normalize_base(raw: dict) -> dict | None:
    """Map BASE.gov.pt / dados.gov.pt anuncio JSON → common schema.

    Actual field names from dados.gov.pt anuncios2026.json:
      nAnuncio, IdIncm, dataPublicacao (DD/MM/YYYY), designacaoEntidade,
      descricaoAnuncio, PrecoBase, CPVs (list "CODE - Desc"), tiposContrato,
      modeloAnuncio, PrazoPropostas (int days), url
    """
    eid = str(raw.get("nAnuncio") or raw.get("IdIncm") or raw.get("id") or "")
    if not eid or eid == "-1":
        return None

    # Title
    title = _clean(raw.get("descricaoAnuncio") or raw.get("titulo") or raw.get("descricao") or "")
    description = title  # descricaoAnuncio IS the description

    # Entity name
    entity_name = _clean(
        raw.get("designacaoEntidade") or raw.get("entidade") or ""
    )

    # CPV — format: "66510000-8 - Serviços de seguros"
    cpv_list = raw.get("CPVs") or raw.get("cpv") or raw.get("cpvs") or []
    cpv = None
    if isinstance(cpv_list, list) and cpv_list:
        first = str(cpv_list[0])
        cpv = first.split("-")[0].strip().split(" ")[0].strip()
    elif isinstance(cpv_list, str):
        cpv = cpv_list.split("-")[0].strip()

    # Dates — dataPublicacao: "DD/MM/YYYY"
    published = _parse_date(raw.get("dataPublicacao") or raw.get("publicacao"))
    # PrazoPropostas is an integer (number of days), not a deadline date
    # No absolute deadline available in this dataset
    deadline = None

    # Value
    val_raw = raw.get("PrecoBase") or raw.get("valorBaseSemIva") or raw.get("valorBase")
    try:
        base_value = float(str(val_raw).replace(",", ".").replace(" ", "")) if val_raw else None
    except:
        base_value = None

    # URL — DR PDF or base.gov link
    url = raw.get("url") or raw.get("link")
    if not url and eid:
        url = f"https://www.base.gov.pt/Base4/pt/detalhe/?type=anuncios&id={eid}"

    return {
        "source":       "base",
        "external_id":  eid,
        "title":        title[:500] if title else None,
        "entity_name":  entity_name[:300] if entity_name else None,
        "description":  description[:2000] if description else None,
        "cpv":          cpv,
        "deadline":     deadline,
        "base_value":   base_value,
        "country":      "PT",
        "region":       None,
        "url":          url,
        "published_at": published,
    }

# ── TED normalizer ────────────────────────────────────────────────────────────

def _normalize_ted(raw: dict) -> dict | None:
    """Map TED v3 notice JSON → common schema."""
    pub_num = raw.get("publication-number") or raw.get("pub_num") or ""
    if not pub_num:
        return None

    # Title from description-glo
    desc_glo = raw.get("description-glo") or {}
    lang_texts = list(desc_glo.values()) if isinstance(desc_glo, dict) else []
    title = ""
    for lt in lang_texts:
        if isinstance(lt, list) and lt:
            title = _clean(str(lt[0]))
            break
        elif isinstance(lt, str):
            title = _clean(lt)
            break
    if not title:
        nature = raw.get("contract-nature") or raw.get("nature") or ""
        if isinstance(nature, list): nature = nature[0] if nature else ""
        title = f"{_clean(str(nature)).capitalize()} — {pub_num}"

    # Nature → description
    nature_val = raw.get("contract-nature") or []
    if isinstance(nature_val, list):
        nature_val = ", ".join(str(n) for n in nature_val)
    description = _clean(str(nature_val))

    # Deadline
    dl_raw = raw.get("deadline-receipt-tender-date-lot")
    if isinstance(dl_raw, list):
        dl_raw = dl_raw[0] if dl_raw else None
    deadline = _parse_date(dl_raw)

    # Value
    val_raw = raw.get("framework-maximum-value-lot")
    if isinstance(val_raw, list):
        val_raw = val_raw[0] if val_raw else None
    try:
        base_value = float(val_raw) if val_raw else None
    except:
        base_value = None

    # PDF URL (Portuguese)
    links = raw.get("links") or {}
    pdf_dict = links.get("pdf") or {}
    url = pdf_dict.get("POR") or pdf_dict.get("ENG") or f"https://ted.europa.eu/pt/notice/{pub_num}/pdf"

    return {
        "source":       "ted",
        "external_id":  pub_num,
        "title":        title[:500] if title else None,
        "entity_name":  None,
        "description":  description[:2000] if description else None,
        "cpv":          None,
        "deadline":     deadline,
        "base_value":   base_value,
        "country":      "PT",
        "region":       None,
        "url":          url,
        "published_at": None,
    }

# ── DR normalizer (stub) ──────────────────────────────────────────────────────

def _normalize_dr(raw: dict) -> dict | None:
    """DR normalizer — stub until API is available."""
    dre_id = str(raw.get("id") or "")
    if not dre_id:
        return None
    return {
        "source":       "dr",
        "external_id":  dre_id,
        "title":        _clean(raw.get("titulo") or "")[:500] or None,
        "entity_name":  _clean(raw.get("entidade") or "")[:300] or None,
        "description":  _clean(raw.get("sumario") or "")[:2000] or None,
        "cpv":          None,
        "deadline":     _parse_date(raw.get("prazo")),
        "base_value":   None,
        "country":      "PT",
        "region":       None,
        "url":          raw.get("url"),
        "published_at": _parse_date(raw.get("data")),
    }

_NORMALIZERS = {"base": _normalize_base, "ted": _normalize_ted, "dr": _normalize_dr}

# ── Main ──────────────────────────────────────────────────────────────────────

def run(source: str, run_id: int | None = None) -> dict:
    normalizer = _NORMALIZERS.get(source)
    if not normalizer:
        raise ValueError(f"Unknown source: {source}")

    with engine.connect() as c:
        # Fetch raw items not yet normalized
        rows = c.execute(sa.text("""
            SELECT ri.id, ri.external_id, ri.payload
            FROM public.radar_raw_items ri
            WHERE ri.source = :src
              AND NOT EXISTS (
                  SELECT 1 FROM public.radar_normalized rn
                  WHERE rn.source = :src AND rn.external_id = ri.external_id
              )
            ORDER BY ri.fetched_at
        """), {"src": source}).mappings().all()

    normalized = 0
    errors = 0
    for row in rows:
        payload = row["payload"] if isinstance(row["payload"], dict) else json.loads(row["payload"] or "{}")
        try:
            rec = normalizer(payload)
            if not rec:
                continue
        except Exception as e:
            errors += 1
            continue

        rec["raw_item_id"] = row["id"]
        rec["run_id"] = run_id
        if rec.get("deadline") is not None:
            rec["deadline"] = rec["deadline"].isoformat() if hasattr(rec["deadline"], "isoformat") else rec["deadline"]
        if rec.get("published_at") is not None:
            rec["published_at"] = rec["published_at"].isoformat() if hasattr(rec["published_at"], "isoformat") else rec["published_at"]

        with engine.begin() as c:
            c.execute(sa.text("""
                INSERT INTO public.radar_normalized
                    (source, external_id, title, entity_name, description, cpv,
                     deadline, base_value, country, region, url, published_at,
                     raw_item_id, run_id)
                VALUES
                    (:source, :external_id, :title, :entity_name, :description, :cpv,
                     :deadline, :base_value, :country, :region, :url, :published_at,
                     :raw_item_id, :run_id)
                ON CONFLICT (source, external_id) DO NOTHING
            """), rec)
        normalized += 1

    print(json.dumps({
        "source": source, "raw_processed": len(rows),
        "normalized": normalized, "errors": errors,
    }))
    return {"normalized": normalized, "errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=["base","ted","dr"])
    parser.add_argument("--run-id", type=int, default=None)
    args = parser.parse_args()
    run(args.source, args.run_id)
