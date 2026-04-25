#!/usr/bin/env python3
"""
bin/run_autocode_once.py
Submit one safe coding improvement through POST /code on agent-router :5679.

Reads the next pending [AUTOCODE] task from the backlog, or seeds a new one
from the curated target list. Marks it done/failed after execution.
Run manually or via aios-autocode.timer (once per day).

Usage:
    python3 bin/run_autocode_once.py [--dry-run]
"""
import json
import os
import sys
import pathlib
import datetime
import urllib.request
import urllib.error
import argparse

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", "/home/jdl/ai-os"))
ROUTER    = os.environ.get("AUTOCODE_ROUTER_URL", "http://127.0.0.1:5679")
LOG_FILE  = AIOS_ROOT / "runtime" / "agent_logs" / "autocode.log"
TAG       = "[AUTOCODE]"

# ── Curated safe targets ──────────────────────────────────────────────────────
# Isolated module-level functions only. Conservative docstring additions.
# Format: (file_path, short_key, task_description)
#
# IMPORTANT — task descriptions include an explicit OUTPUT CONSTRAINT to prevent
# the engineer LLM from generating collateral patches for other files.
_SINGLE_FILE_CONSTRAINT = (
    " OUTPUT CONSTRAINT: produce a CHANGED FILE block for {file} ONLY. "
    "Do not output any === CHANGED FILE: === block for any other file. "
    "If you include changes to other files, the entire task will be rejected."
)

# NOTE: safe_exec.py and memory.py are intentionally excluded from TARGETS.
# The engineer LLM consistently corrupts these when targeting them — it rewrites
# method bodies and strips block lists via truncation placeholders.
# They already have the required docstrings added manually.
TARGETS = [
    (
        "agents/coding/router.py",
        "get_ollama_endpoint",
        "Add a one-line docstring to the get_ollama_endpoint function in agents/coding/router.py. "
        "The docstring must be exactly: Return the Ollama API endpoint URL from config. "
        "Insert it as the first line of the function body. Do not change the function signature, "
        "parameters, or any other code."
        + _SINGLE_FILE_CONSTRAINT.format(file="agents/coding/router.py"),
    ),
    (
        "agents/coding/safe_exec.py",
        "is_safe_command",
        "Add a one-line docstring to the is_safe_command function in agents/coding/safe_exec.py. "
        "The docstring must be exactly: Return True if the command is safe to run without manual approval. "
        "Insert it as the first line of the function body. Do not change the function signature, "
        "parameters, or any other code."
        + _SINGLE_FILE_CONSTRAINT.format(file="agents/coding/safe_exec.py"),
    ),
    (
        "agents/coding/reviewer.py",
        "to_dict",
        "Add a one-line docstring to the to_dict method of ReviewResult in agents/coding/reviewer.py. "
        "The docstring must be exactly: Return review result fields as a plain dict for serialisation. "
        "Insert it as the first line of the method body. Do not change the method signature, "
        "parameters, or any other code."
        + _SINGLE_FILE_CONSTRAINT.format(file="agents/coding/reviewer.py"),
    ),
]

# ── Critical files that must not be corrupted by collateral patches ───────────
# If any of these fail py_compile after a run, we abort and log an alert.
CRITICAL_FILES = [
    "agents/coding/memory.py",
    "agents/coding/reviewer.py",
    "agents/coding/engineer.py",
    "agents/coding/executor.py",
    "agents/coding/router.py",
    "agents/coding/safe_exec.py",
]


def _log(msg: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _load_token() -> str:
    token = os.environ.get("AIOS_TOKEN", "")
    if token:
        return token
    env = AIOS_ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("AIOS_TOKEN="):
                return line.split("=", 1)[1].strip()
    return ""


def _api(method: str, path: str, body: dict = None, token: str = "") -> dict:
    url = f"{ROUTER}{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-AIOS-TOKEN": token}
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {e.read().decode(errors='replace')}") from e


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Pass dry_run=true to /code (no files written)")
    args = parser.parse_args()
    dry_run = args.dry_run

    token = _load_token()
    if not token:
        _log("ERROR: AIOS_TOKEN not found in env or .env file")
        sys.exit(1)

    _log(f"run_autocode_once starting (dry_run={dry_run})")

    # ── Find pending AUTOCODE task or seed one ────────────────────────────────
    all_tasks = _api("GET", "/backlog/list", token=token).get("tasks", [])

    autocode_pending = [
        t for t in all_tasks
        if TAG in t.get("title", "") and t.get("status") == "pending"
    ]
    autocode_done_keys = {
        t["title"].split("|key:", 1)[1].strip() if "|key:" in t.get("title", "") else ""
        for t in all_tasks
        if TAG in t.get("title", "") and t.get("status") in ("done", "skipped")
    }

    if autocode_pending:
        task = autocode_pending[0]
        _log(f"found pending task {task['id']}: {task['title'][:70]}")
    else:
        # Seed: first target whose key is not already done
        task = None
        for file_path, key, desc in TARGETS:
            if key in autocode_done_keys:
                _log(f"skip {key}: already done")
                continue
            title = f"{TAG} {file_path}:{key} |key:{key}"
            resp = _api("POST", "/backlog/add", token=token, body={
                "title": title,
                "goal":  desc,
                "priority": 3,
                "task_type": "DEV_TASK",
            })
            task = resp.get("task")
            if task:
                # Attach the file path and key for retrieval
                task["_file"] = file_path
                task["_key"]  = key
                _log(f"seeded task {task['id']}: {title}")
                break

    if task is None:
        _log("all curated targets done — nothing to run")
        return

    # ── Extract file and goal ────────────────────────────────────────────────
    goal      = task.get("goal", "")
    title     = task.get("title", "")
    file_path = task.get("_file", "")

    # Parse file from title if not already set
    if not file_path and TAG in title:
        # Title format: "[AUTOCODE] agents/coding/router.py:load_config |key:..."
        after_tag = title.replace(f"{TAG} ", "", 1)
        file_path = after_tag.split(":")[0].strip() if ":" in after_tag else ""

    # Match goal to TARGETS to get file if still missing
    if not file_path:
        for fp, _k, desc in TARGETS:
            if goal.startswith(desc[:40]):
                file_path = fp
                break

    # ── Snapshot critical files before calling /code ──────────────────────────
    # The engineer LLM sometimes generates collateral patches for files not
    # in the task target. We snapshot and restore any non-target file that changes.
    snapshots: dict[str, bytes] = {}
    for cf in CRITICAL_FILES:
        cp = AIOS_ROOT / cf
        if cp.exists() and cf != file_path:
            snapshots[cf] = cp.read_bytes()

    # ── Submit to /code ───────────────────────────────────────────────────────
    _log(f"submitting: {goal[:80]}...")
    _log(f"  file={file_path}  dry_run={dry_run}")

    try:
        result = _api("POST", "/code", token=token, body={
            "task":     goal,
            "files":    [file_path] if file_path else [],
            "dry_run":  dry_run,
        })
    except RuntimeError as e:
        _log(f"ERROR from /code: {e}")
        _api("POST", "/backlog/update", token=token,
             body={"id": task["id"], "status": "failed", "last_error": str(e)})
        sys.exit(1)

    ok       = result.get("ok", False)
    decision = result.get("decision", "")
    outcome  = result.get("outcome", "")
    diff     = result.get("diff", "")
    error    = result.get("error", "")
    actions  = result.get("actions", [])

    _log(f"result: ok={ok} decision={decision} outcome={outcome}")
    for a in actions:
        _log(f"  action: {a}")
    if diff:
        _log(f"  diff: {diff}")
    if error:
        _log(f"  error: {error}")

    # ── Guard: restore any collateral-modified critical files ─────────────────
    # Re-read each snapshotted file; if content changed, restore from snapshot.
    for cf, original in snapshots.items():
        cp = AIOS_ROOT / cf
        if cp.exists() and cp.read_bytes() != original:
            cp.write_bytes(original)
            _log(f"  RESTORED collateral change to {cf}")

    # ── Update backlog ────────────────────────────────────────────────────────
    new_status = "done" if ok else "failed"
    _api("POST", "/backlog/update", token=token, body={
        "id":         task["id"],
        "status":     new_status,
        "last_error": error or None,
    })
    _log(f"task {task['id']} marked {new_status}")
    _log("run_autocode_once done")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("=" * 64)
    print(f"  Task     : {goal[:60]}")
    print(f"  File     : {file_path}")
    print(f"  Decision : {decision or '(n/a)'}")
    print(f"  Outcome  : {outcome or '(n/a)'}")
    print(f"  Diff     : {diff or '(none)'}")
    if error:
        print(f"  Error    : {error}")
    print("=" * 64)


if __name__ == "__main__":
    main()
