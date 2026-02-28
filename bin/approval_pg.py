#!/usr/bin/env python3
"""
AI-OS Approval Workflow — Postgres + audit_log.
Substitui runtime/pending_approvals.json.
Fallback para JSON se DB indisponível.

CLI:
  python3 approval_pg.py list                    → aprovações pendentes
  python3 approval_pg.py approve <id> [user_id]  → aprova
  python3 approval_pg.py reject  <id> [user_id]  → rejeita
  python3 approval_pg.py request <json>           → cria aprovação pendente
"""
import os, sys, json, time, pathlib, datetime, hashlib, importlib.util

AIOS_ROOT     = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
APPROVALS_FILE = AIOS_ROOT / "runtime" / "pending_approvals.json"

# ── DB loader ────────────────────────────────────────────────────────────────

_db = None

def _get_db():
    global _db
    if _db is None:
        spec = importlib.util.spec_from_file_location("db", AIOS_ROOT / "bin" / "db.py")
        _db  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_db)
    return _db


# ── JSON fallback helpers ─────────────────────────────────────────────────────

def _j_load() -> list:
    if APPROVALS_FILE.exists():
        try:
            return json.loads(APPROVALS_FILE.read_text())
        except Exception:
            pass
    return []


def _j_save(data: list):
    APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    APPROVALS_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _input_hash(inp: dict) -> str:
    return hashlib.sha256(json.dumps(inp, sort_keys=True).encode()).hexdigest()[:16]


# ── Core functions ────────────────────────────────────────────────────────────

def request_approval(tool: str, inp: dict, job_id: str = "",
                     step_id: int | None = None, user_id: int | None = None) -> dict:
    """
    Regista aprovação pendente na DB e no ficheiro JSON.
    Retorna o entry com id, status=pending.
    """
    apr_id   = f"apr_{int(time.time())}_{_input_hash(inp)}"
    inp_json = json.dumps(inp, ensure_ascii=False)

    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            # regista na tabela approvals
            a = db_mod.Approval(
                id=apr_id, tool=tool, input=inp_json,
                status="pending", job_id=job_id or None,
            )
            s.add(a)
            # se existe step, marca como pending_approval
            if step_id:
                step = s.query(db_mod.Step).filter(db_mod.Step.id == step_id).first()
                if step:
                    step.status = "pending_approval"
            # pausa o job
            if job_id:
                job = s.query(db_mod.Job).filter(db_mod.Job.id == job_id).first()
                if job:
                    job.status = "waiting_approval"
            s.commit()
            # audit
            db_mod.audit(s, "approval_requested",
                         user_id=user_id, resource=tool,
                         detail=f"id={apr_id} job={job_id}")
        finally:
            s.close()
    except Exception as e:
        pass  # log silencioso, continua com JSON

    # mantém JSON em sincronia (para UI legado)
    entry = {"id": apr_id, "tool": tool, "input": inp,
             "job_id": job_id, "status": "pending",
             "requested_at": int(time.time())}
    data = _j_load()
    data.append(entry)
    _j_save(data)

    return entry


def check_approved(tool: str, inp: dict) -> bool:
    """Verifica se existe aprovação para este tool+input."""
    try:
        db_mod  = _get_db()
        s       = db_mod.SessionLocal()
        inp_json = json.dumps(inp, ensure_ascii=False)
        try:
            a = (
                s.query(db_mod.Approval)
                .filter(
                    db_mod.Approval.tool   == tool,
                    db_mod.Approval.input  == inp_json,
                    db_mod.Approval.status == "approved",
                )
                .first()
            )
            return a is not None
        finally:
            s.close()
    except Exception:
        # fallback JSON
        for a in _j_load():
            if (a.get("tool") == tool and a.get("status") == "approved"
                    and a.get("input") == inp):
                return True
        return False


def approve(approval_id: str, user_id: int | None = None) -> dict:
    """Aprova uma aprovação pendente. Retoma o job."""
    now = datetime.datetime.utcnow()
    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            a = s.query(db_mod.Approval).filter(db_mod.Approval.id == approval_id).first()
            if not a:
                return {"ok": False, "error": "não encontrado"}
            if a.status != "pending":
                return {"ok": False, "error": f"estado inválido: {a.status}"}
            a.status      = "approved"
            a.resolved_at = now
            a.resolved_by = user_id
            # retoma job
            if a.job_id:
                job = s.query(db_mod.Job).filter(db_mod.Job.id == a.job_id).first()
                if job and job.status == "waiting_approval":
                    job.status = "pending"
            s.commit()
            db_mod.audit(s, "approval_approved",
                         user_id=user_id, resource=a.tool,
                         detail=f"id={approval_id} job={a.job_id}")
        finally:
            s.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    # sincronia JSON
    data = _j_load()
    for item in data:
        if item.get("id") == approval_id:
            item["status"] = "approved"
    _j_save(data)

    return {"ok": True, "id": approval_id, "status": "approved"}


def reject(approval_id: str, user_id: int | None = None) -> dict:
    """Rejeita aprovação pendente."""
    now = datetime.datetime.utcnow()
    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            a = s.query(db_mod.Approval).filter(db_mod.Approval.id == approval_id).first()
            if not a:
                return {"ok": False, "error": "não encontrado"}
            a.status      = "rejected"
            a.resolved_at = now
            a.resolved_by = user_id
            if a.job_id:
                job = s.query(db_mod.Job).filter(db_mod.Job.id == a.job_id).first()
                if job and job.status == "waiting_approval":
                    job.status = "failed"
                    job.last_error = "Aprovação rejeitada"
            s.commit()
            db_mod.audit(s, "approval_rejected",
                         user_id=user_id, resource=a.tool,
                         detail=f"id={approval_id}")
        finally:
            s.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}

    data = _j_load()
    for item in data:
        if item.get("id") == approval_id:
            item["status"] = "rejected"
    _j_save(data)

    return {"ok": True, "id": approval_id, "status": "rejected"}


def list_pending() -> list:
    """Lista aprovações pendentes."""
    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            rows = (
                s.query(db_mod.Approval)
                .filter(db_mod.Approval.status == "pending")
                .order_by(db_mod.Approval.requested_at.desc())
                .all()
            )
            return [
                {
                    "id":     r.id,
                    "tool":   r.tool,
                    "input":  json.loads(r.input) if r.input else {},
                    "job_id": r.job_id,
                    "status": r.status,
                    "requested_at": int(r.requested_at.timestamp()) if r.requested_at else 0,
                }
                for r in rows
            ]
        finally:
            s.close()
    except Exception:
        return [a for a in _j_load() if a.get("status") == "pending"]


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "list":
        print(json.dumps({"approvals": list_pending()}))

    elif cmd == "approve" and len(sys.argv) >= 3:
        uid = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print(json.dumps(approve(sys.argv[2], user_id=uid)))

    elif cmd == "reject" and len(sys.argv) >= 3:
        uid = int(sys.argv[3]) if len(sys.argv) > 3 else None
        print(json.dumps(reject(sys.argv[2], user_id=uid)))

    elif cmd == "request" and len(sys.argv) >= 3:
        p = json.loads(sys.argv[2])
        print(json.dumps(request_approval(
            tool=p.get("tool", "unknown"),
            inp=p.get("input", {}),
            job_id=p.get("job_id", ""),
        )))

    else:
        print(json.dumps({"ok": False, "error": f"uso: list|approve|reject|request"}))
