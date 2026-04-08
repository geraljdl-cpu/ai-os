#!/usr/bin/env python3
"""
cluster_worker.py — AI-OS Cluster Node Worker
Consome jobs da fila public.worker_jobs por role.
Versão: 2026-03-15 — Fase 2

Fluxo:
  hostname → config/cluster_workers.json → roles[]
  loop: lease(kind∈roles) → dispatch → report → log

Env vars (override do config):
  DATABASE_URL    — override DB URL
  AIOS_ROOT       — root path (default: /cluster/d1/ai-os)
  AIOS_NODE_NAME  — override hostname
  AIOS_POLL_SEC   — poll interval em segundos (default: 5)
"""
import sys, os

# ── sys.path: pylib NFS (pg8000+sqlalchemy) + remover bin/ para não shadow stdlib ──
_ROOT   = os.environ.get("AIOS_ROOT", "/cluster/d1/ai-os")
_PYLIB  = os.path.join(_ROOT, "pylib")
_BINDIR = os.path.join(_ROOT, "bin")
if os.path.isdir(_PYLIB) and _PYLIB not in sys.path:
    sys.path.insert(0, _PYLIB)
if _BINDIR in sys.path:
    sys.path.remove(_BINDIR)

import json, time, socket, subprocess, traceback, logging, pathlib, urllib.request
from datetime import datetime

# ── Logging (stdout + ficheiro NFS) ─────────────────────────────────────────
NODE_NAME = os.environ.get("AIOS_NODE_NAME", socket.gethostname())
_LOG_DIR  = pathlib.Path(os.path.join(_ROOT, "runtime", "workers", NODE_NAME))
_LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(_LOG_DIR / "worker.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger("cluster_worker")

# ── Config ───────────────────────────────────────────────────────────────────
_CFG = json.loads(pathlib.Path(os.path.join(_ROOT, "config", "cluster_workers.json")).read_text())

_node   = _CFG["nodes"].get(NODE_NAME, {})
ROLES   = _node.get("roles", ["general", "fallback"])
_db     = _CFG.get("db", {})

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+pg8000://{user}:{pass}@{host}:{port}/{name}".format(**_db)
)
OLLAMA_URL  = _CFG.get("ollama_url", "http://192.168.1.172:11434")
POLL_SEC    = float(os.environ.get("AIOS_POLL_SEC", str(_CFG.get("poll_sec", 5))))
JOB_TIMEOUT = int(_CFG.get("job_timeout_secs", 300))

# ── Secrets (NFS .secrets file → os.environ) ─────────────────────────────────
_SECRETS = os.path.join(_ROOT, "config", ".secrets")
if os.path.isfile(_SECRETS):
    for _ln in open(_SECRETS):
        _ln = _ln.strip()
        if _ln and not _ln.startswith("#") and "=" in _ln:
            _k, _v = _ln.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

log.info(f"node={NODE_NAME}  roles={ROLES}  db={_db.get('host')}:{_db.get('port')}")

# ── DB ───────────────────────────────────────────────────────────────────────
def _make_engine():
    from sqlalchemy import create_engine
    return create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=0,
        connect_args={"timeout": 10},
    )

def _wait_db():
    from sqlalchemy import text
    for attempt in range(36):   # ~6 min
        try:
            eng = _make_engine()
            with eng.connect() as c:
                c.execute(text("SELECT 1"))
            log.info("DB conectado.")
            return eng
        except Exception as e:
            log.warning(f"DB indisponível ({attempt+1}/36): {e}")
            time.sleep(10)
    log.error("Timeout DB. A sair.")
    sys.exit(1)

# ── SQL ───────────────────────────────────────────────────────────────────────
_LEASE_SQL = """
    UPDATE public.worker_jobs
       SET status             = 'running',
           assigned_worker_id = :wid,
           ts_assigned        = NOW()
     WHERE id = (
               SELECT id FROM public.worker_jobs
                WHERE status = 'queued'
                  AND (target_worker_id IS NULL OR target_worker_id = :wid)
                  AND (kind = ANY(:roles) OR target_worker_id = :wid)
             ORDER BY ts_created ASC
                LIMIT 1
           FOR UPDATE SKIP LOCKED
           )
 RETURNING id, kind, payload
"""

_REPORT_SQL = """
    UPDATE public.worker_jobs
       SET status  = :status,
           result  = :result,
           ts_done = NOW()
     WHERE id = :id
"""

_HEARTBEAT_SQL = """
    INSERT INTO public.workers (id, hostname, role, status, last_seen)
         VALUES (:id, :hostname, :role, :status, NOW())
    ON CONFLICT (id) DO UPDATE
           SET hostname  = EXCLUDED.hostname,
               role      = EXCLUDED.role,
               status    = EXCLUDED.status,
               last_seen = NOW()
"""

def _lease(conn, text_fn):
    row = conn.execute(text_fn(_LEASE_SQL), {
        "wid": NODE_NAME, "roles": ROLES
    }).mappings().first()
    return dict(row) if row else None

def _report(conn, text_fn, job_id, status, result):
    r = json.dumps(result) if not isinstance(result, str) else result
    conn.execute(text_fn(_REPORT_SQL), {"id": job_id, "status": status, "result": r})
    conn.commit()

# ── Heartbeat ─────────────────────────────────────────────────────────────────
_hb_last = 0.0

def _heartbeat(eng, text_fn, status="idle"):
    global _hb_last
    if time.time() - _hb_last < 30:
        return
    _hb_last = time.time()
    try:
        with eng.begin() as c:
            c.execute(text_fn(_HEARTBEAT_SQL), {
                "id": NODE_NAME, "hostname": NODE_NAME,
                "role": ",".join(ROLES), "status": status,
            })
    except Exception as e:
        log.warning(f"Heartbeat: {e}")

# ── Handlers ──────────────────────────────────────────────────────────────────

def _run_shell(cmd: str, timeout: int = JOB_TIMEOUT) -> dict:
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    out  = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        raise RuntimeError(f"exit={proc.returncode}: {out[:600]}")
    return {"output": out[:4000], "exit_code": 0}


def handle_echo(p):
    return {"echo": p, "node": NODE_NAME, "roles": ROLES}

def handle_shell(p):
    cmd = p.get("cmd") or p.get("command", "")
    if not cmd:
        raise ValueError("payload.cmd em falta")
    log.info(f"SHELL: {cmd[:120]}")
    return _run_shell(cmd)

def handle_ai_analysis(p):
    # Case 1: idea thread analysis → runs idea_router.py (Claude API)
    thread_id = p.get("thread_id")
    if thread_id is not None:
        log.info(f"IDEA ANALYSIS: thread_id={thread_id}")
        agents = p.get("agents", "")
        cmd = f"python3 {_ROOT}/bin/idea_router.py {int(thread_id)}"
        if agents:
            cmd += " " + " ".join(agents if isinstance(agents, list) else agents.split())
        return _run_shell(cmd, timeout=360)

    # Case 2: direct prompt via local Ollama
    prompt = p.get("prompt", "")
    if not prompt:
        if p.get("cmd") or p.get("command"):
            return handle_shell(p)
        raise ValueError("payload.thread_id, payload.prompt ou payload.cmd em falta")
    model = p.get("model", "qwen2.5:14b")
    log.info(f"AI: model={model} prompt[:60]={prompt[:60]}")
    body = json.dumps({"model": model, "prompt": prompt,
                       "stream": False, "options": {"temperature": 0.3}}).encode()
    req  = urllib.request.Request(f"{OLLAMA_URL}/api/generate", data=body,
                                  headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return {"response": data.get("response", ""), "model": model}

def handle_radar(p):
    # pylib in PYTHONPATH (inherited from service env) → do NOT add bin/ (shadows stdlib secrets)
    script = p.get("script", "")
    if script:
        cmd = f"python3 {_ROOT}/bin/{script}.py"
        if p.get("args"):
            cmd += " " + " ".join(str(a) for a in p["args"])
        return _run_shell(cmd)
    return handle_shell(p)

def handle_preprocess(p):
    return handle_shell(p)

def handle_automation(p):
    return handle_shell(p)

def handle_watchdog(p):
    target = p.get("target", "127.0.0.1")
    port   = int(p.get("port", 80))
    import socket as _s
    try:
        with _s.create_connection((target, port), timeout=5):
            return {"ok": True,  "target": target, "port": port}
    except Exception as e:
        return {"ok": False, "target": target, "port": port, "error": str(e)}

def handle_light(p):
    sub = p.get("sub", "echo")
    if sub == "shell":
        return handle_shell(p)
    return handle_echo(p)

def handle_fallback(p):
    """Aceita qualquer job quando não há worker especializado disponível"""
    return handle_shell(p) if p.get("cmd") or p.get("command") else handle_echo(p)

HANDLERS = {
    "echo":         handle_echo,
    "shell":        handle_shell,
    "ai_analysis":  handle_ai_analysis,
    "radar":        handle_radar,
    "preprocess":   handle_preprocess,
    "automation":   handle_automation,
    "watchdog":     handle_watchdog,
    "light":        handle_light,
    "general":      handle_shell,
    "fallback":     handle_fallback,
}

def dispatch(kind: str, payload: dict) -> dict:
    fn = HANDLERS.get(kind, handle_fallback)
    return fn(payload)

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    from sqlalchemy import text as T

    eng = _wait_db()
    _heartbeat(eng, T, "online")

    log.info(f"Worker pronto. Poll={POLL_SEC}s  roles={ROLES}")

    while True:
        _heartbeat(eng, T, "idle")
        try:
            with eng.begin() as conn:
                job = _lease(conn, T)

            if not job:
                time.sleep(POLL_SEC)
                continue

            job_id  = job["id"]
            kind    = job.get("kind") or "shell"
            payload = job.get("payload") or {}
            if isinstance(payload, str):
                try:   payload = json.loads(payload)
                except Exception: payload = {"raw": payload}

            log.info(f">>> Job {job_id} kind={kind}")
            _heartbeat(eng, T, "working")

            t0 = time.monotonic()
            try:
                result  = dispatch(kind, payload)
                status  = "done"
                elapsed = time.monotonic() - t0
                log.info(f"<<< Job {job_id} done em {elapsed:.1f}s")
            except Exception as exc:
                elapsed = time.monotonic() - t0
                log.error(f"<<< Job {job_id} failed em {elapsed:.1f}s: {exc}")
                result  = {"error": str(exc), "traceback": traceback.format_exc()[-600:]}
                status  = "failed"

            with eng.begin() as conn:
                _report(conn, T, job_id, status, result)

        except KeyboardInterrupt:
            log.info("Shutdown.")
            break
        except Exception as e:
            log.error(f"Loop error: {e}")
            time.sleep(POLL_SEC)

if __name__ == "__main__":
    main()
