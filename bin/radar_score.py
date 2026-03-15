#!/usr/bin/env python3
"""
radar_score.py — Scoring Engine
Reads radar_normalized for a given source and writes scores to radar_scores.

Usage:
    python3 bin/radar_score.py --source base [--run-id N]
    python3 bin/radar_score.py --source ted  [--run-id N]
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, json
from datetime import date

import sqlalchemy as sa

DB_URL = _os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
engine = sa.create_engine(DB_URL)

SCORE_THRESHOLD = 15

# ── Keyword groups (same as radar_ted.py) ────────────────────────────────────

KEYWORD_GROUPS = [
    {
        "name": "reciclagem_cabos",
        "keywords": ["reciclagem","cabo","cobre","granulacao","granulação",
                     "sucata","residuo","residuos","metal","fio"],
        "base_score": 20,
        "cpv_prefixes": ["14","24","38","90"],   # raw materials, chemicals, recycling
    },
    {
        "name": "gestao_residuos",
        "keywords": ["residuos","residuo","REEE","valorizacao","valorização",
                     "tratamento","deposito","depósito","recolha","aterro"],
        "base_score": 10,
        "cpv_prefixes": ["90"],
    },
    {
        "name": "manutencao_eletrica",
        "keywords": ["eletrica","elétrica","instalacoes","instalações","quadro",
                     "transformador","cabo","condutor","subestacao","baixa tensao"],
        "base_score": 10,
        "cpv_prefixes": ["31","45"],
    },
    {
        "name": "obras_industriais",
        "keywords": ["industrial","fabrica","fábrica","armazem","armazém",
                     "pavilhao","pavilhão","maquinaria","equipamento"],
        "base_score": 8,
        "cpv_prefixes": ["45"],
    },
]

def _score(norm: dict) -> tuple[int, list, str | None]:
    """Returns (score, reasons, best_group_name)."""
    text = " ".join(filter(None, [
        (norm.get("title") or "").lower(),
        (norm.get("description") or "").lower(),
    ]))
    cpv = (norm.get("cpv") or "")[:2]  # first 2 digits for group match

    best_score = 0
    best_reasons: list = []
    best_group: str | None = None

    for grp in KEYWORD_GROUPS:
        score = 0
        reasons: list = []

        # Keyword match in title/description
        for kw in grp["keywords"]:
            if kw.lower() in text:
                score += 10
                reasons.append(f"keyword:{kw}(+10)")

        if score == 0:
            continue  # no keywords matched this group

        score += grp["base_score"]
        reasons.append(f"group:{grp['name']}(+{grp['base_score']})")

        # CPV bonus
        if cpv and any(cpv.startswith(p) for p in grp["cpv_prefixes"]):
            score += 8
            reasons.append(f"cpv:{cpv}(+8)")

        # Value bonus
        val = norm.get("base_value")
        if val:
            try:
                v = float(val)
                if v >= 100_000:
                    score += 15; reasons.append("value≥100k(+15)")
                elif v >= 50_000:
                    score += 10; reasons.append("value≥50k(+10)")
                elif v >= 10_000:
                    score += 5;  reasons.append("value≥10k(+5)")
            except: pass

        # Deadline urgency
        dl = norm.get("deadline")
        if dl:
            try:
                if isinstance(dl, str):
                    from datetime import date as _date
                    dl = _date.fromisoformat(dl)
                days = (dl - date.today()).days
                if days < 0:
                    score -= 20; reasons.append("expired(-20)")
                elif days <= 30:
                    score += 8; reasons.append(f"deadline_{days}d(+8)")
            except: pass

        if score > best_score:
            best_score = score
            best_reasons = reasons
            best_group = grp["name"]

    return best_score, best_reasons, best_group


def _priority(score: int) -> str:
    if score >= 30: return "high"
    if score >= 15: return "medium"
    return "low"


def run(source: str, run_id: int | None = None) -> dict:
    with engine.connect() as c:
        rows = c.execute(sa.text("""
            SELECT rn.id, rn.external_id, rn.title, rn.description,
                   rn.cpv, rn.deadline, rn.base_value
            FROM public.radar_normalized rn
            WHERE rn.source = :src
              AND NOT EXISTS (
                  SELECT 1 FROM public.radar_scores rs
                  WHERE rs.source = :src AND rs.external_id = rn.external_id
              )
        """), {"src": source}).mappings().all()

    scored = 0
    skipped = 0
    for row in rows:
        norm = dict(row)
        score, reasons, group = _score(norm)

        if score < SCORE_THRESHOLD:
            skipped += 1
            continue

        priority = _priority(score)
        with engine.begin() as c:
            c.execute(sa.text("""
                INSERT INTO public.radar_scores
                    (source, external_id, normalized_id, group_name, score,
                     score_reasons, priority, run_id)
                VALUES
                    (:source, :external_id, :normalized_id, :group_name, :score,
                     :score_reasons, :priority, :run_id)
                ON CONFLICT (source, external_id) DO UPDATE
                    SET score=EXCLUDED.score, score_reasons=EXCLUDED.score_reasons,
                        priority=EXCLUDED.priority
            """), {
                "source": source,
                "external_id": norm["external_id"],
                "normalized_id": norm["id"],
                "group_name": group,
                "score": score,
                "score_reasons": json.dumps(reasons),
                "priority": priority,
                "run_id": run_id,
            })
        scored += 1

    print(json.dumps({
        "source": source, "candidates": len(rows),
        "scored": scored, "below_threshold": skipped,
    }))
    return {"scored": scored, "skipped": skipped}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=["base","ted","dr"])
    parser.add_argument("--run-id", type=int, default=None)
    args = parser.parse_args()
    run(args.source, args.run_id)
