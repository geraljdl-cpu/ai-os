import threading
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("autopilot")

DEFAULT_REPO   = "/host/ai-os"
DEFAULT_BRANCH = "main"
DEFAULT_TESTS  = "python -m compileall -q ."
LOOP_INTERVAL  = int(os.environ.get("AUTOPILOT_EVERY_SECONDS", "30"))
TASK_TIMEOUT   = int(os.environ.get("AUTOPILOT_TASK_TIMEOUT", "300"))
LOCK_FILE      = Path("/app/runtime/autopilot.lock")

_stop_event = threading.Event()

def _acquire_lock() -> bool:
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        if LOCK_FILE.exists():
            age = time.time() - LOCK_FILE.stat().st_mtime
            if age > TASK_TIMEOUT + 60:
                LOCK_FILE.unlink()
            else:
                return False
        LOCK_FILE.write_text(str(os.getpid()))
        return True
    except Exception as ex:
        log.error("[autopilot] lock error: " + str(ex))
        return False

def _release_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass

def _run_next(jobs_fn, backlog_mod, ops_fn=None, research_fn=None):
    if not _acquire_lock():
        return
    task = backlog_mod.get_next_task()
    if not task:
        _release_lock()
        return
    task_id = task["id"]
    log.info("[autopilot] running task " + task_id + ": " + task["title"])
    backlog_mod.update_task(task_id, status="running", attempts=task.get("attempts", 0) + 1)
    try:
        container = [None]
        def run():
            task_type = task.get("type", "DEV_TASK")
            if task_type == "OPS_TASK":
                container[0] = ops_fn(task["goal"])
            elif task_type == "RESEARCH_TASK":
                container[0] = research_fn(task["goal"])
            else:
                container[0] = jobs_fn({
                    "repo_path":   DEFAULT_REPO,
                    "base_branch": DEFAULT_BRANCH,
                    "test_cmd":    DEFAULT_TESTS,
                    "request":     task["goal"],
                })
        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout=TASK_TIMEOUT)
        if t.is_alive():
            backlog_mod.update_task(task_id, status="failed", last_error="timeout")
            _release_lock()
            return
        result = container[0] or {"ok": False, "error": "no result"}
        job_id = result.get("job_id")
        if job_id:
            backlog_mod.update_task(task_id, last_job_id=job_id, last_job_dir=result.get("job_dir", ""))
        if result.get("ok"):
            backlog_mod.update_task(task_id, status="done", last_error=None)
            log.info("[autopilot] task " + task_id + " DONE")
            _self_trigger(task, result, backlog_mod)
        else:
            err = result.get("error", "unknown")
            status = "skipped" if "no diff" in err else "failed"
            backlog_mod.update_task(task_id, status=status, last_error=err)
            log.warning("[autopilot] task " + task_id + " " + status.upper() + ": " + err)
    except Exception as ex:
        backlog_mod.update_task(task_id, status="failed", last_error=str(ex))
        log.error("[autopilot] task " + task_id + " EXCEPTION: " + str(ex))
    finally:
        _release_lock()

def _self_trigger(task, result, backlog_mod):
    import urllib.request, json as _json, re as _re
    prompt = ("Task done: " + task["title"] + ". Goal was: " + task["goal"] +
              ". Suggest up to 2 follow-up tasks as JSON array with fields title, goal, type. Reply ONLY with valid JSON array.")
    try:
        url = "http://127.0.0.1:5679/agent"
        data = _json.dumps({"chatInput": prompt, "mode": "openai"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = _json.loads(r.read())
            answer = resp.get("answer", "")
            m = _re.search(r"\[.*\]", answer, _re.S)
            tasks = _json.loads(m.group(0)) if m else []
            for t in tasks[:2]:
                backlog_mod.add_task(title=t["title"], goal=t["goal"], task_type=t.get("type", "DEV_TASK"))
                log.info("[self-trigger] added: " + t["title"])
    except Exception as ex:
        log.warning("[self-trigger] skipped: " + str(ex))

def _ops_fn(goal: str) -> dict:
    import urllib.request, json
    url = "http://bash-bridge:8020/run"
    data = json.dumps({"cmd": goal.split()}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"ok": True, "result": json.loads(r.read())}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

def _research_fn(goal: str, agent_url: str = "http://127.0.0.1:5679/agent") -> dict:
    import urllib.request, json
    data = json.dumps({"chatInput": goal, "mode": "openai"}).encode()
    req = urllib.request.Request(agent_url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
            return {"ok": True, "answer": resp.get("answer", "")}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

def _loop(jobs_fn, backlog_mod, ops_fn, research_fn):
    log.info("[autopilot] loop started interval=" + str(LOOP_INTERVAL) + "s")
    while not _stop_event.is_set():
        try:
            _run_next(jobs_fn, backlog_mod, ops_fn, research_fn)
        except Exception as ex:
            log.error("[autopilot] loop error: " + str(ex))
        _stop_event.wait(LOOP_INTERVAL)
    log.info("[autopilot] loop stopped")

def start(jobs_fn, backlog_mod):
    _release_lock()
    t = threading.Thread(target=_loop, args=(jobs_fn, backlog_mod, _ops_fn, _research_fn), daemon=True)
    t.start()
    return t

def stop():
    _stop_event.set()
    _release_lock()
