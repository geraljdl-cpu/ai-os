import threading
import logging

log = logging.getLogger("autopilot")

DEFAULT_REPO   = "/host/ai-os"
DEFAULT_BRANCH = "main"
DEFAULT_TESTS  = "python -m compileall -q ."
LOOP_INTERVAL  = 30

_stop_event = threading.Event()

def _run_next(jobs_fn, backlog_mod, ops_fn=None, research_fn=None):
    task = backlog_mod.get_next_task()
    if not task:
        return
    task_id = task["id"]
    log.info(f"[autopilot] running task {task_id}: {task['title']}")
    backlog_mod.update_task(task_id, status="running", attempts=task.get("attempts", 0) + 1)
    try:
        task_type = task.get("type", "DEV_TASK")
        if task_type == "OPS_TASK":
            result = ops_fn(task["goal"])
        elif task_type == "RESEARCH_TASK":
            result = research_fn(task["goal"])
        else:
            result = jobs_fn({
                "repo_path":   DEFAULT_REPO,
                "base_branch": DEFAULT_BRANCH,
                "test_cmd":    DEFAULT_TESTS,
                "request":     task["goal"],
            })
        job_id = result.get("job_id")
        if job_id:
            backlog_mod.update_task(task_id, last_job_id=job_id, last_job_dir=result.get("job_dir",""))
        if result.get("ok"):
            backlog_mod.update_task(task_id, status="done", last_error=None)
            log.info(f"[autopilot] task {task_id} DONE")
            _self_trigger(task, result, backlog_mod)
            log.info(f"[autopilot] task {task_id} DONE branch={result.get('branch')}")
        else:
            err = result.get("error", "unknown")
            status = "skipped" if "no diff" in err else "failed"
            backlog_mod.update_task(task_id, status=status, last_error=err)
            log.warning(f"[autopilot] task {task_id} {status.upper()}: {err}")
    except Exception as ex:
        backlog_mod.update_task(task_id, status="failed", last_error=str(ex))
        log.error(f"[autopilot] task {task_id} EXCEPTION: {ex}")

def _loop(jobs_fn, backlog_mod, ops_fn, research_fn):
    log.info("[autopilot] loop started")
    while not _stop_event.is_set():
        try:
            _run_next(jobs_fn, backlog_mod, _ops_fn, _research_fn)
        except Exception as ex:
            log.error(f"[autopilot] loop error: {ex}")
        _stop_event.wait(LOOP_INTERVAL)

def _self_trigger(task, result, backlog_mod):
    import urllib.request, json as _json
    prompt = "Task done: " + task["title"] + ". Goal was: " + task["goal"] + ". Suggest up to 2 follow-up tasks as JSON array with fields title, goal, type. Reply ONLY with valid JSON array."
    try:
        url = "http://127.0.0.1:5679/agent"
        data = _json.dumps({"chatInput": prompt, "mode": "openai"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = _json.loads(r.read())
            answer = resp.get("answer", "")
            import re as _re
            m = _re.search(r"\[.*\]", answer, _re.S)
            tasks = _json.loads(m.group(0)) if m else []
            for t in tasks[:2]:
                backlog_mod.add_task(title=t["title"], goal=t["goal"], task_type=t.get("type","DEV_TASK"))
                log.info("[self-trigger] added: " + t["title"])
    except Exception as ex:
        log.warning("[self-trigger] skipped: " + str(ex))

def _ops_fn(goal: str) -> dict:
    """Executa comandos bash directamente via bash-bridge."""
    import urllib.request, json
    url = "http://bash-bridge:8020/run"
    data = json.dumps({"cmd": goal.split()}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return {"ok": True, "result": json.loads(r.read())}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

def _research_fn(goal: str, agent_url: str = "http://127.0.0.1:5679/agent") -> dict:
    """Envia goal ao agente e devolve resposta."""
    import urllib.request, json
    data = json.dumps({"chatInput": goal, "mode": "openai"}).encode()
    req = urllib.request.Request(agent_url, data=data, headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            resp = json.loads(r.read())
            return {"ok": True, "answer": resp.get("answer","")}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

def start(jobs_fn, backlog_mod):
    t = threading.Thread(target=_loop, args=(jobs_fn, backlog_mod, _ops_fn, _research_fn), daemon=True)
    t.start()
    return t

def stop():
    _stop_event.set()
