"""
agents/coding/memory.py — Append-only persistent memory and logging.

Stores tasks, plans, review decisions, execution results.
Format: JSONL (one JSON object per line) for easy grep/tail.
Also writes human-readable Markdown summaries.
"""

import json
import os
import pathlib
import datetime

AIOS_ROOT = pathlib.Path(os.environ.get("AIOS_ROOT", os.path.expanduser("~/ai-os")))
MEMORY_DIR = AIOS_ROOT / "runtime" / "agent_memory"
LOG_DIR    = AIOS_ROOT / "runtime" / "agent_logs"

def _ensure_dirs():
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

def _now() -> str:
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def _append_jsonl(path: pathlib.Path, record: dict):
    """Append a JSON record to the given JSONL file, creating it if absent."""
    _ensure_dirs()
    with open(path, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

class MemoryLog:
    """
    Per-task memory context.
    All writes are append-only. Nothing is ever deleted or overwritten.
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.memory_file = MEMORY_DIR / f"{task_id}.jsonl"
        self.log_file    = LOG_DIR    / "coding_agent.jsonl"

    def log_task(self, description: str, context: dict = None):
        record = {
            "ts": _now(), "task_id": self.task_id,
            "event": "task_received", "description": description,
            "context": context or {},
        }
        _append_jsonl(self.memory_file, record)
        _append_jsonl(self.log_file, record)

    def log_plan(self, plan: str, model: str):
        record = {
            "ts": _now(), "task_id": self.task_id,
            "event": "plan_generated", "model": model,
            "plan": plan,
        }
        _append_jsonl(self.memory_file, record)
        _append_jsonl(self.log_file, record)

    def log_review(self, review: str, decision: str):
        record = {
            "ts": _now(), "task_id": self.task_id,
            "event": "review_completed", "decision": decision,
            "review": review,
        }
        _append_jsonl(self.memory_file, record)
        _append_jsonl(self.log_file, record)

    def log_execution(self, result: str):
        record = {
            "ts": _now(), "task_id": self.task_id,
            "event": "execution_completed", "result": result,
        }
        _append_jsonl(self.memory_file, record)
        _append_jsonl(self.log_file, record)

    def read_task_history(self) -> list[dict]:
        if not self.memory_file.exists():
            return []
        records = []
        for line in self.memory_file.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        return records

def tail_log(n: int = 20) -> list[dict]:
    """Return the last n entries from the global coding agent log."""
    log_file = LOG_DIR / "coding_agent.jsonl"
    if not log_file.exists():
        return []
    lines = log_file.read_text().splitlines()
    records = []
    for line in lines[-n:]:
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records

if __name__ == "__main__":
    import sys
    entries = tail_log(int(sys.argv[1]) if len(sys.argv) > 1 else 20)
    for e in entries:
        print(json.dumps(e))