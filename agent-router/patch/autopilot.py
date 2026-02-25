import threading
import logging

log = logging.getLogger("autopilot")

DEFAULT_REPO   = "/host/ai-os"
DEFAULT_BRANCH = "main"
DEFAULT_TESTS  = "python -m compileall -q ."
LOOP_INTERVAL  = 30

_stop_event = threading.Event()

def _run_next(jobs_fn, backlog_mod):
    task = backlog_mod.get_next_task()
    if not task:
        return
    task_id = task["id"]
    log.info(f"[autopilot] running task {task_id}: {task['title']}")
    backlog_mod.update_task(task_id, status="running", attempts=task.get("attempts", 0) + 1)
    try:
        result = jobs_fn({
            "repo_path":   DEFAULT_REPO,
            "base_branch": DEFAULT_BRANCH,
            "test_cmd":    DEFAULT_TESTS,
            "request":     task["goal"],
        })
        if result.get("ok"):
            backlog_mod.update_task(task_id, status="done", last_error=None)
            log.info(f"[autopilot] task {task_id} DONE branch={result.get('branch')}")
        else:
            err = result.get("error", "unknown")
            status = "skipped" if "no diff" in err else "failed"
            backlog_mod.update_task(task_id, status=status, last_error=err)
            log.warning(f"[autopilot] task {task_id} {status.upper()}: {err}")
    except Exception as ex:
        backlog_mod.update_task(task_id, status="failed", last_error=str(ex))
        log.error(f"[autopilot] task {task_id} EXCEPTION: {ex}")

def _loop(jobs_fn, backlog_mod):
    log.info("[autopilot] loop started")
    while not _stop_event.is_set():
        try:
            _run_next(jobs_fn, backlog_mod)
        except Exception as ex:
            log.error(f"[autopilot] loop error: {ex}")
        _stop_event.wait(LOOP_INTERVAL)

def start(jobs_fn, backlog_mod):
    t = threading.Thread(target=_loop, args=(jobs_fn, backlog_mod), daemon=True)
    t.start()
    return t

def stop():
    _stop_event.set()
