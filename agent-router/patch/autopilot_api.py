"""
autopilot_api.py — endpoints HTTP para controlar o autopilot
Colar no app.py: from autopilot_api import register; register(app)
"""
import time
from typing import Any

_state = {
    "running": False,
    "loop_interval": 30,
    "last_tick": None,
    "current_task_id": None,
    "last_error": None,
    "counts": {"done": 0, "failed": 0, "skipped": 0, "total": 0},
}

def update_state(**kwargs):
    _state.update(kwargs)

def inc_count(key: str):
    _state["counts"][key] = _state["counts"].get(key, 0) + 1
    _state["counts"]["total"] = _state["counts"].get("total", 0) + 1

def register(app, autopilot_mod, jobs_fn, backlog_mod):
    from fastapi import Header, HTTPException

    @app.get("/autopilot/status")
    def autopilot_status():
        return _state

    @app.post("/autopilot/start")
    def autopilot_start():
        if not _state["running"]:
            autopilot_mod.start(jobs_fn, backlog_mod)
            _state["running"] = True
        return {"ok": True, "running": True}

    @app.post("/autopilot/stop")
    def autopilot_stop():
        autopilot_mod.stop()
        _state["running"] = False
        return {"ok": True, "running": False}

    @app.post("/autopilot/tick")
    def autopilot_tick():
        """Executa 1 ciclo imediatamente (útil para debug)."""
        from jobs_ai import new_job
        import backlog as _bk
        _state["last_tick"] = int(time.time())
        autopilot_mod._run_next(jobs_fn, backlog_mod, autopilot_mod._ops_fn, autopilot_mod._research_fn)
        return {"ok": True, "tick": _state["last_tick"]}
