#!/usr/bin/env python3
"""
bin/coding_service.py — Standalone HTTP service wrapping the AI-OS coding subsystem.

This is the nodecpu-resident invocation path for the coding pipeline.
It exposes the Engineer → Reviewer → Executor pipeline as a JSON REST API so that
agent-router, n8n, UI, and other callers can submit coding tasks over HTTP.

Runs on nodecpu host (127.0.0.1:5680 by default).
LLM inference is delegated to nodegpu at http://192.168.1.202:11434 (Ollama).

Usage:
  /home/jdl/ai-os/.venv/bin/python3 bin/coding_service.py
  /home/jdl/ai-os/.venv/bin/python3 bin/coding_service.py --port 5680 --host 127.0.0.1

Start via systemd:
  systemctl start aios-coding

Endpoints:
  GET  /health          — liveness + routing info (no auth)
  GET  /status?n=20     — tail recent log entries
  POST /code            — run full pipeline
                          body: {"task": "...", "files": ["rel/path.py"], "dry_run": false}
"""
import argparse
import datetime
import os
import pathlib
import sys
import uuid

# bin/ is auto-added to sys.path because this script lives there.
# Remove it immediately: bin/secrets.py would otherwise shadow the stdlib 'secrets' module,
# which starlette imports at startup.
_this_dir = str(pathlib.Path(__file__).parent.resolve())
if _this_dir in sys.path:
    sys.path.remove(_this_dir)

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
sys.path.insert(0, str(AIOS_ROOT))

from agents.coding.router import get_routing_info, load_config  # noqa: E402
from agents.coding.engineer import Engineer                       # noqa: E402
from agents.coding.reviewer import Reviewer                       # noqa: E402
from agents.coding.executor import Executor                       # noqa: E402
from agents.coding.memory import tail_log                         # noqa: E402

from fastapi import FastAPI  # noqa: E402
from pydantic import BaseModel  # noqa: E402
import uvicorn  # noqa: E402

app = FastAPI(title="AI-OS Coding Service", version="1.0")

DEFAULT_PORT = int(os.environ.get("CODING_SERVICE_PORT", "5680"))


def _make_task_id() -> str:
    ts = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return f"task_{ts}_{uuid.uuid4().hex[:6]}"


class CodeReq(BaseModel):
    task: str
    files: list[str] = []
    dry_run: bool = False


@app.get("/health")
def health():
    """Liveness check + routing info. Does NOT call Ollama."""
    try:
        routing = get_routing_info()
        return {
            "ok": True,
            "endpoint": routing["endpoint"],
            "coding_model": routing["models"].get("coding"),
            "aios_root": str(AIOS_ROOT),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/status")
def status(n: int = 20):
    """Return last n entries from the global coding agent log."""
    entries = tail_log(n)
    return {"entries": entries, "count": len(entries)}


@app.post("/code")
def code(req: CodeReq):
    """
    Run the full coding pipeline for the given task.

    The pipeline is synchronous and may take 30–240 s depending on model load.
    Set dry_run=true to exercise Engineer+Reviewer without writing any files.

    Returns:
      ok:       true if pipeline completed without error
      task_id:  unique ID for this run (maps to runtime/agent_memory/{task_id}.jsonl)
      approved: true if reviewer approved the plan
      outcome:  "completed" | "dry_run" | "skipped" | "failed"
      actions:  list of actions taken (or planned, in dry_run mode)
      diff:     line-count summary per file
    """
    if not req.task.strip():
        return {"ok": False, "error": "task is required"}

    task_id = _make_task_id()
    cfg = load_config()

    # ── Engineer ──────────────────────────────────────────────────────────────
    engineer = Engineer(task_id)
    try:
        eng_result = engineer.plan_with_patch(req.task, req.files)
    except RuntimeError as e:
        return {"ok": False, "task_id": task_id, "stage": "engineer", "error": str(e)}

    plan = eng_result["plan"]
    patches = eng_result["patches"]

    # ── Reviewer ──────────────────────────────────────────────────────────────
    reviewer = Reviewer(task_id)
    try:
        review = reviewer.review(req.task, plan, patches)
    except RuntimeError as e:
        return {"ok": False, "task_id": task_id, "stage": "reviewer", "error": str(e)}

    if not review.approved:
        return {
            "ok": True,
            "task_id": task_id,
            "approved": False,
            "decision": review.decision,
            "reasons": review.reasons,
            "plan": plan,
            "patches": list(patches.keys()),
        }

    # ── Executor ──────────────────────────────────────────────────────────────
    executor = Executor(task_id, dry_run=req.dry_run)

    post_cmds = []
    if cfg.get("safety", {}).get("run_post_validation") and patches:
        for rel_path in patches:
            if rel_path.endswith(".py"):
                post_cmds.append(f"python3 -m py_compile {AIOS_ROOT / rel_path}")

    exec_result = executor.execute(
        req.task,
        review,
        patches=patches,
        post_validate_cmd=post_cmds[0] if post_cmds else None,
    )

    return {
        "ok": exec_result.outcome in ("completed", "dry_run"),
        "task_id": task_id,
        "approved": True,
        "decision": review.decision,
        "reasons": review.reasons,
        "outcome": exec_result.outcome,
        "actions": exec_result.actions,
        "diff": exec_result.diff,
        "dry_run": req.dry_run,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AI-OS Coding Service")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="TCP port (default 5680)")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default 127.0.0.1)")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
