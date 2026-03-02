#!/usr/bin/env python3
"""
AI-OS Backlog — Postgres com fallback para JSON.
Drop-in replacement de agent-router/patch/backlog.py para o host.
API compatível: list_tasks, add_task, get_next_task, update_task
Extras: cleanup_old_jobs (archive > 30 dias), status CLI para server.js.
"""
import os, sys, json, uuid, time, pathlib, datetime, importlib.util

AIOS_ROOT   = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
BACKLOG_FILE = AIOS_ROOT / "runtime" / "backlog.json"
JOBS_DIR     = AIOS_ROOT / "runtime" / "jobs"

ARCHIVE_DAYS = int(os.environ.get("AIOS_ARCHIVE_DAYS", "30"))

VALID_TYPES = {"DEV_TASK", "OPS_TASK", "RESEARCH_TASK"}
TYPE_MAP    = {"research": "RESEARCH_TASK", "practical": "DEV_TASK",
               "dev": "DEV_TASK", "ops": "OPS_TASK"}


# ── DB loader (lazy) ─────────────────────────────────────────────────────────

_db = None

def _get_db():
    global _db
    if _db is None:
        spec = importlib.util.spec_from_file_location("db", AIOS_ROOT / "bin" / "db.py")
        _db  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_db)
    return _db


def _db_ok() -> bool:
    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        s.execute(db_mod.engine.dialect.has_table.__func__ and
                  __import__("sqlalchemy").text("SELECT 1"))
        s.close()
        return True
    except Exception:
        return False


# ── JSON fallback ─────────────────────────────────────────────────────────────

def _j_load() -> dict:
    if BACKLOG_FILE.exists():
        try:
            return json.loads(BACKLOG_FILE.read_text())
        except Exception:
            pass
    return {"tasks": []}


def _j_save(data: dict):
    BACKLOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(BACKLOG_FILE) + ".tmp"
    pathlib.Path(tmp).write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, str(BACKLOG_FILE))


def _j_to_dict(t: dict) -> dict:
    return {
        "id":         t.get("id", ""),
        "title":      t.get("title") or t.get("goal", "")[:80],
        "goal":       t.get("goal", ""),
        "status":     t.get("status", "pending"),
        "priority":   t.get("priority", 5),
        "task_type":  t.get("type", "DEV_TASK"),
        "attempts":   t.get("attempts", 0),
        "last_error": t.get("last_error"),
        "created_at": t.get("created_at", int(time.time())),
        "updated_at": t.get("updated_at", int(time.time())),
    }


# ── DB helpers ────────────────────────────────────────────────────────────────

def _row_to_dict(job) -> dict:
    def _ts(dt):
        return int(dt.timestamp()) if isinstance(dt, datetime.datetime) else (dt or 0)
    return {
        "id":         job.id,
        "title":      job.title or "",
        "goal":       job.goal or "",
        "status":     job.status,
        "priority":   job.priority or 5,
        "task_type":  job.task_type or "DEV_TASK",
        "attempts":   job.attempts or 0,
        "last_error": job.last_error,
        "created_at": _ts(job.created_at),
        "updated_at": _ts(job.updated_at),
    }


# ── Public API ────────────────────────────────────────────────────────────────

def list_tasks(include_archived=False) -> list:
    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            q = s.query(db_mod.Job)
            if not include_archived:
                q = q.filter(db_mod.Job.status != "archived")
            return [_row_to_dict(j) for j in q.order_by(db_mod.Job.created_at.desc()).limit(200)]
        finally:
            s.close()
    except Exception:
        return [_j_to_dict(t) for t in _j_load().get("tasks", [])]


def add_task(title: str, goal: str, priority: int = 5,
             task_type: str = "DEV_TASK") -> dict:
    task_type = TYPE_MAP.get(task_type, task_type)
    if task_type not in VALID_TYPES:
        task_type = "DEV_TASK"
    job_id = uuid.uuid4().hex[:12]

    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            job = db_mod.Job(
                id=job_id, title=title, goal=goal,
                status="pending", priority=int(priority),
                task_type=task_type, attempts=0,
            )
            s.add(job)
            s.commit()
            result = _row_to_dict(job)
        finally:
            s.close()
    except Exception:
        # fallback JSON
        result = {
            "id": job_id, "title": title, "goal": goal,
            "status": "pending", "priority": int(priority),
            "task_type": task_type, "attempts": 0, "last_error": None,
            "created_at": int(time.time()), "updated_at": int(time.time()),
        }
        data = _j_load()
        data["tasks"].append({**result, "type": task_type})
        _j_save(data)

    # mantém JSON em sincronia
    _sync_to_json(result)
    return result


def get_next_task() -> dict | None:
    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            job = (
                s.query(db_mod.Job)
                .filter(db_mod.Job.status == "pending")
                .order_by(db_mod.Job.priority, db_mod.Job.created_at)
                .first()
            )
            return _row_to_dict(job) if job else None
        finally:
            s.close()
    except Exception:
        data = _j_load()
        tasks = data.get("tasks", [])

        # filtra pending e escolhe pelo mesmo critério
        pending = []
        for t in tasks:
            if (t.get("status") or "pending") == "pending":
                pending.append(_j_to_dict(t))

        if not pending:
            return None

        picked = sorted(pending, key=lambda x: (x.get("priority", 999), x.get("created_at", 0)))[0]
        picked_id = picked.get("id")

        # CLAIM/ACK no JSON: marca running e grava
        now = int(time.time())
        for t in tasks:
            if t.get("id") == picked_id:
                t["status"] = "running"
                t["updated_at"] = now
                break
        data["tasks"] = tasks
        _j_save(data)

        return picked


def update_task(task_id: str, **fields) -> dict | None:
    # normaliza field names (backlog.py compat)
    if "type" in fields:
        fields["task_type"] = fields.pop("type")

    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            job = s.query(db_mod.Job).filter(db_mod.Job.id == task_id).first()
            if not job:
                return None
            for k, v in fields.items():
                if hasattr(job, k):
                    setattr(job, k, v)
            job.updated_at = datetime.datetime.utcnow()
            s.commit()
            result = _row_to_dict(job)
        finally:
            s.close()
    except Exception:
        # fallback JSON
        data = _j_load()
        for t in data["tasks"]:
            if t.get("id") == task_id:
                t.update(fields)
                t["updated_at"] = int(time.time())
                _j_save(data)
                return _j_to_dict(t)
        return None

    _sync_to_json(result)
    return result


def cleanup_old_jobs():
    """Arquiva jobs com mais de ARCHIVE_DAYS dias."""
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=ARCHIVE_DAYS)
    archived = 0
    try:
        db_mod = _get_db()
        s = db_mod.SessionLocal()
        try:
            old = (
                s.query(db_mod.Job)
                .filter(
                    db_mod.Job.created_at < cutoff,
                    db_mod.Job.status.in_(["done", "failed", "skipped"]),
                )
                .all()
            )
            for job in old:
                job.status = "archived"
                archived += 1
            s.commit()
        finally:
            s.close()
    except Exception:
        pass
    return archived


def _sync_to_json(task: dict):
    """Mantém backlog.json actualizado para compatibilidade com server.js antigo."""
    try:
        data   = _j_load()
        tasks  = data.get("tasks", [])
        exists = next((i for i, t in enumerate(tasks) if t.get("id") == task["id"]), None)
        entry  = {**task, "type": task.get("task_type", "DEV_TASK")}
        if exists is not None:
            tasks[exists] = entry
        else:
            tasks.append(entry)
        data["tasks"] = tasks
        _j_save(data)
    except Exception:
        pass


# ── Status para server.js ─────────────────────────────────────────────────────

def get_status() -> dict:
    tasks  = list_tasks()
    pending  = [t for t in tasks if t["status"] == "pending"]
    running  = [t for t in tasks if t["status"] == "running"]
    done     = [t for t in tasks if t["status"] == "done"]
    failed   = [t for t in tasks if t["status"] == "failed"]
    waiting  = [t for t in tasks if t["status"] == "waiting_approval"]

    # jobs no formato esperado pelo server.js antigo
    jobs = [{"id": t["id"], "goal": t["goal"] or t["title"],
             "status": t["status"]} for t in tasks[:20]]

    return {
        "ok":      True,
        "tasks":   tasks[:50],
        "jobs":    jobs,
        "counts":  {
            "pending": len(pending), "running": len(running),
            "done": len(done), "failed": len(failed),
            "waiting_approval": len(waiting),
        },
        "status":  "WORKING" if running else "READY",
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


# --- JSON-forced helpers (source of truth: runtime/backlog.json) ---
def _j_find_and_update(task_id: str, **fields):
    d = _j_load()
    tasks = d.get("tasks", [])
    hit = None
    for t in tasks:
        if t.get("id") == task_id:
            t.update(fields)
            hit = t
            break
    if hit is not None:
        _j_save(d)
    return _j_to_dict(hit) if hit else None

def update_task_json(task_id: str, **fields) -> dict | None:
    """Atualiza task no backlog.json (ignora Postgres)."""
    return _j_find_and_update(task_id, **fields)

def get_next_task_json() -> dict | None:
    """Claim da próxima task pending no backlog.json (ignora Postgres)."""
    d = _j_load()
    tasks = d.get("tasks", [])
    pending = [t for t in tasks if (t.get("status") or "pending") == "pending"]
    if not pending:
        return None
    # escolhe por priority/created_at se existir, senão ordem natural
    def _k(t):
        return (int(t.get("priority", 999)), int(t.get("created_at", 0)))
    picked = sorted(pending, key=_k)[0]
    picked["status"] = "running"
    import time as _time
    picked["updated_at"] = int(_time.time())
    _j_save(d)
    return _j_to_dict(picked)

if __name__ == "__main__":
    cmd    = sys.argv[1] if len(sys.argv) > 1 else "status"
    params = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    if cmd == "status":
        print(json.dumps(get_status()))
    elif cmd == "add":
        print(json.dumps(add_task(
            title=params.get("title", params.get("goal", "?")),
            goal=params.get("goal", ""),
            priority=params.get("priority", 5),
            task_type=params.get("task_type", "DEV_TASK"),
        )))
    elif cmd == "list":
        print(json.dumps({"tasks": list_tasks()}))
    elif cmd == "cleanup":
        n = cleanup_old_jobs()
        print(json.dumps({"ok": True, "archived": n}))
    elif cmd == "next":
        t = get_next_task()
        print(json.dumps(t or {}))
    else:
        print(json.dumps({"ok": False, "error": f"unknown: {cmd}"}))
# ---------------------------------------------------------------------
# SAFE UTILITIES (peek + requeue stuck running)
# ---------------------------------------------------------------------

import time
from typing import Optional, Dict, Any, List, Tuple

def _as_int_ts(v):
    if v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


def peek_next_task_json(task_type: str = "DEV_TASK") -> Optional[Dict[str, Any]]:
    """
    Vê a próxima task pending SEM dar claim.
    """
    try:
        tasks = list_tasks()
    except Exception:
        return None

    pendings = []
    for t in tasks or []:
        if t.get("status") != "pending":
            continue
        if task_type and t.get("task_type") != task_type:
            continue
        pendings.append(t)

    if not pendings:
        return None

    def key(t):
        pr = int(t.get("priority", 5))
        ca = _as_int_ts(t.get("created_at")) or 0
        return (pr, ca)

    pendings.sort(key=key)
    return pendings[0]


def requeue_stuck_running(max_age_secs: int = 900) -> List[Dict[str, Any]]:
    """
    Volta a pending tasks presas em running.
    """
    now = int(time.time())
    try:
        tasks = list_tasks()
    except Exception:
        return []

    reset = []
    for t in tasks or []:
        if t.get("status") != "running":
            continue

        ts = _as_int_ts(t.get("updated_at")) or _as_int_ts(t.get("created_at"))
        if not ts:
            continue

        if now - ts > max_age_secs:
            update_task_json(t["id"], status="pending", last_error="requeued stuck running")
            reset.append(t)

    return reset


def autofill_if_empty(n: int = 4) -> int:
    """
    Enche o backlog com tarefas housekeeping seguras quando não há tasks pending.
    Só usa ferramentas presentes na allowlist (echo, cat, python3, ls, find, wc).
    NÃO cria tarefas com SQL/mysql/psql/npm/git/CREATE/DROP.
    Não chama o worker — apenas cria tasks.
    Retorna quantas tasks foram criadas.
    """
    # Não enfileira se já há pending
    tasks = list_tasks()
    if any(t.get("status") == "pending" for t in tasks):
        return 0

    # Lista fixa de tarefas housekeeping executáveis no stack actual.
    # Goals escritos para o agent-router: só bash_safe/python3/write_file.
    HOUSEKEEPING: list[dict] = [
        {
            "title": "Ops: aiosctl status",
            "goal": (
                "Verificar bin/aiosctl e garantir que o comando 'status' mostra: "
                "service aios-worker activo, contagem de tasks pending/done/failed do Postgres, "
                "e o id do último job. Usar python3 para ler backlog_pg. "
                "Não usar npm, git, mysql, psql, sed ou CREATE."
            ),
            "task_type": "OPS_TASK",
        },
        {
            "title": "Ops: worker heartbeat",
            "goal": (
                "Garantir que autopilot_loop.sh escreve o timestamp UTC em "
                "runtime/worker.last_seen em cada ciclo. "
                "Usar python3 -c para escrever o ficheiro (não usar sed, awk ou git). "
                "Exemplo: python3 -c \"import datetime,pathlib; "
                "pathlib.Path('runtime/worker.last_seen')"
                ".write_text(datetime.datetime.utcnow().isoformat())\" . "
                "Verificar que o ficheiro existe após a escrita com cat."
            ),
            "task_type": "OPS_TASK",
        },
        {
            "title": "Docs: README arquitectura",
            "goal": (
                "Criar ou actualizar README.md (máx 60 linhas) com: "
                "componentes do AI-OS (Express porta 3000, agent-router 5679, "
                "Postgres 5432, Modbus 5020, Art-Net 6454), "
                "flow do autopilot (backlog → worker → tools_engine → Postgres), "
                "e comandos de operação (aiosctl, systemctl --user, journalctl). "
                "Usar write_file ou python3 para escrever. Não usar git ou npm."
            ),
            "task_type": "DEV_TASK",
        },
        {
            "title": "Cleanup: jobs antigos",
            "goal": (
                "Criar script bin/cleanup_jobs.sh que lista (dry-run por default) "
                "directorias em runtime/jobs/ com mais de 30 dias usando find. "
                "Com argumento --delete apaga-as. "
                "Usar apenas find, echo, wc e rm (sem npm, git, mysql ou psql). "
                "Escrever o script com python3 write_file ou cat, torná-lo executável "
                "com python3 -c 'import os; os.chmod(\"bin/cleanup_jobs.sh\", 0o755)'."
            ),
            "task_type": "OPS_TASK",
        },
    ]

    catalogue = HOUSEKEEPING[:int(n)]
    created = 0
    for spec in catalogue:
        add_task(
            title=spec["title"],
            goal=spec["goal"],
            priority=6,
            task_type=spec.get("task_type", "OPS_TASK"),
        )
        created += 1
    return created
