#!/usr/bin/env python3
"""
radar_twin_bridge.py — Twin Bridge
ONLY writer to twin_entities / twin_cases / twin_tasks / events for radar sources.

Dedup key: (source, external_id) — checked via metadata->>'source' AND metadata->>'external_id'

Usage:
    python3 bin/radar_twin_bridge.py --source base [--run-id N]
    python3 bin/radar_twin_bridge.py --source ted  [--run-id N]
"""
import sys as _sys, os as _os
_bin_dir = _os.path.dirname(_os.path.abspath(__file__))
if _bin_dir in _sys.path:
    _sys.path.remove(_bin_dir)

import argparse, json
from datetime import date as _date

import sqlalchemy as sa

DB_URL = _os.environ.get("DATABASE_URL", "postgresql://aios_user:jdl@127.0.0.1:5432/aios")
engine = sa.create_engine(DB_URL)

# ── Dedup ─────────────────────────────────────────────────────────────────────

def _find_entity(c, text, source: str, external_id: str) -> int | None:
    """Returns entity_id if already in twin, else None.
    Primary dedup key: metadata->>'source' = :src AND metadata->>'external_id' = :eid
    Fallback for legacy TED entries: metadata->>'pub_num' = :eid (no source field yet)
    """
    row = c.execute(text("""
        SELECT id FROM public.twin_entities
        WHERE type = 'tender'
          AND metadata->>'source'      = :src
          AND metadata->>'external_id' = :eid
        LIMIT 1
    """), {"src": source, "eid": external_id}).mappings().first()
    if row:
        return row["id"]
    # Fallback: legacy TED entries stored before source/external_id fields were added
    if source == "ted":
        row = c.execute(text("""
            SELECT id FROM public.twin_entities
            WHERE type = 'tender'
              AND metadata->>'pub_num' = :eid
              AND (metadata->>'source' IS NULL OR metadata->>'source' = 'ted')
            LIMIT 1
        """), {"eid": external_id}).mappings().first()
    return row["id"] if row else None

# ── Bridge ────────────────────────────────────────────────────────────────────

def _ensure_doc_requests(c, text, case_id: int, tender_deadline: str | None):
    """Para cada requisito documental de contexto 'tender', verificar se a empresa
    tem o documento válido. Criar document_request para os que estão em falta/expirados.
    Usa company_id=1 (empresa principal) como owner por omissão."""
    COMPANY_OWNER_TYPE = "company"
    COMPANY_OWNER_ID   = 1
    due = tender_deadline[:10] if tender_deadline else None

    reqs = c.execute(text("""
        SELECT id, doc_type, max_age_days FROM public.document_requirements
        WHERE context_type = 'tender' AND target_type = 'company'
    """)).mappings().all()

    for req in reqs:
        # Check if valid doc exists
        age_filter = ""
        if req["max_age_days"]:
            age_filter = f"AND issue_date >= CURRENT_DATE - interval '{req['max_age_days']} days'"
        existing = c.execute(text(f"""
            SELECT id FROM public.documents
            WHERE owner_type = :ot AND owner_id = :oid
              AND doc_type = :dt
              AND status NOT IN ('expired', 'missing')
              AND expiry_date > CURRENT_DATE
              {age_filter}
            LIMIT 1
        """), {"ot": COMPANY_OWNER_TYPE, "oid": COMPANY_OWNER_ID, "dt": req["doc_type"]}).first()

        if existing:
            continue  # doc válido existe — nada a fazer

        # Check if request already open for this context+doc
        already = c.execute(text("""
            SELECT id FROM public.document_requests
            WHERE owner_type = :ot AND owner_id = :oid
              AND doc_type = :dt AND linked_case_id = :cid
              AND status NOT IN ('done', 'failed')
            LIMIT 1
        """), {"ot": COMPANY_OWNER_TYPE, "oid": COMPANY_OWNER_ID,
               "dt": req["doc_type"], "cid": case_id}).first()

        if already:
            continue

        c.execute(text("""
            INSERT INTO public.document_requests
              (requirement_id, owner_type, owner_id, doc_type, status,
               process_type, linked_case_id, due_date)
            VALUES (:rid, :ot, :oid, :dt, 'open', 'tender_intake', :cid, :due)
        """), {"rid": req["id"], "ot": COMPANY_OWNER_TYPE, "oid": COMPANY_OWNER_ID,
               "dt": req["doc_type"], "cid": case_id, "due": due})


def run(source: str, run_id: int | None = None) -> dict:
    text = sa.text

    # Load scored items not yet in twin (or needing update)
    with engine.connect() as c:
        rows = c.execute(text("""
            SELECT rs.source, rs.external_id, rs.score, rs.group_name,
                   rs.score_reasons, rs.priority,
                   rn.title, rn.entity_name, rn.description,
                   rn.cpv, rn.deadline, rn.base_value,
                   rn.url, rn.published_at, rn.region
            FROM public.radar_scores rs
            JOIN public.radar_normalized rn
              ON rn.source = rs.source AND rn.external_id = rs.external_id
            WHERE rs.source = :src
        """), {"src": source}).mappings().all()

    created = 0
    updated = 0

    for row in rows:
        src        = row["source"]
        eid        = row["external_id"]
        score      = row["score"]
        group_name = row["group_name"]
        title      = row["title"] or f"{src.upper()} — {eid}"

        # Build metadata
        meta = {
            "source":      src,
            "external_id": eid,
            "pub_num":     eid,           # backwards-compat with existing TED tenders
            "title":       title,
            "score":       score,
            "grupo":       group_name,
            "nature":      None,
            "deadline":    str(row["deadline"]) if row["deadline"] else "",
            "pdf_url":     row["url"] or "",
            "estado":      "novo",        # default; preserved on update
            "cpv":         row["cpv"],
            "entity_name": row["entity_name"],
            "base_value":  float(row["base_value"]) if row["base_value"] else None,
            "region":      row["region"],
        }

        with engine.begin() as c:
            existing_id = _find_entity(c, text, src, eid)

            if existing_id:
                # Update: refresh score + fields, but keep estado
                cur_meta_row = c.execute(text(
                    "SELECT metadata FROM public.twin_entities WHERE id=:id"
                ), {"id": existing_id}).mappings().first()
                cur_meta = cur_meta_row["metadata"] if cur_meta_row else {}
                if isinstance(cur_meta, str):
                    cur_meta = json.loads(cur_meta or "{}")

                # Preserve estado
                meta["estado"] = cur_meta.get("estado", "novo")

                c.execute(text(
                    "UPDATE public.twin_entities SET metadata=:m, updated_at=NOW() WHERE id=:id"
                ), {"m": json.dumps(meta), "id": existing_id})

                c.execute(text("""
                    INSERT INTO public.events (ts, level, source, kind, entity_id, message, data)
                    VALUES (NOW(), 'info', 'twin', 'tender_updated', :eid, :msg, :data)
                """), {
                    "eid": existing_id,
                    "msg": f"Tender {src.upper()} #{eid} actualizado — score {score}",
                    "data": json.dumps({"source": src, "external_id": eid, "score": score}),
                })
                updated += 1

            else:
                # Create: entity + case + task + event
                entity_id = c.execute(text("""
                    INSERT INTO public.twin_entities (tenant_id, type, name, status, metadata)
                    VALUES ('jdl', 'tender', :name, 'active', :meta)
                    RETURNING id
                """), {"name": title[:200], "meta": json.dumps(meta)}).scalar()

                case_id = c.execute(text("""
                    INSERT INTO public.twin_cases (tenant_id, workflow_key, entity_id, status, data)
                    VALUES ('jdl', 'tender_intake', :eid, 'open', :data)
                    RETURNING id
                """), {
                    "eid": entity_id,
                    "data": json.dumps({"source": src, "external_id": eid, "score": score}),
                }).scalar()

                c.execute(text("""
                    INSERT INTO public.twin_tasks (case_id, title, type, status)
                    VALUES (:cid, 'Analisar elegibilidade e decidir candidatura', 'human', 'pending')
                """), {"cid": case_id})

                c.execute(text("""
                    INSERT INTO public.events (ts, level, source, kind, entity_id, message, data)
                    VALUES (NOW(), 'info', 'twin', 'tender_detected', :eid, :msg, :data)
                """), {
                    "eid": entity_id,
                    "msg": f"Radar {src.upper()}: concurso detectado — score {score} — {eid}",
                    "data": json.dumps({"source": src, "external_id": eid, "score": score, "grupo": group_name}),
                })

                # Auto-checklist: criar doc_requests para docs em falta/expirados
                _ensure_doc_requests(c, text, case_id, meta.get("deadline"))

                created += 1

    # Update radar_runs if run_id given
    if run_id:
        with engine.begin() as c:
            c.execute(text("""
                UPDATE public.radar_runs
                SET twin_created = twin_created + :cr,
                    twin_updated = twin_updated + :up
                WHERE id = :rid
            """), {"cr": created, "up": updated, "rid": run_id})

    print(json.dumps({"source": source, "created": created, "updated": updated}))
    return {"created": created, "updated": updated}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=["base","ted","dr"])
    parser.add_argument("--run-id", type=int, default=None)
    args = parser.parse_args()
    run(args.source, args.run_id)
