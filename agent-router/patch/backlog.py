import json
import uuid
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

RUNTIME = Path("/app/runtime")
BACKLOG_FILE = RUNTIME / "backlog.json"

def _now() -> int:
    return int(time.time())

def _load() -> Dict[str, Any]:
    if not BACKLOG_FILE.exists():
        return {"tasks": []}
    return json.loads(BACKLOG_FILE.read_text(encoding="utf-8"))

def _save(data: Dict[str, Any]) -> None:
    BACKLOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    BACKLOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

def list_tasks() -> List[Dict[str, Any]]:
    return _load()["tasks"]

def add_task(title: str, goal: str, priority: int = 5) -> Dict[str, Any]:
    data = _load()
    task = {
        "id": uuid.uuid4().hex[:8],
        "title": title,
        "goal": goal,
        "priority": int(priority),
        "status": "pending",
        "created_at": _now(),
        "updated_at": _now(),
        "attempts": 0,
        "last_error": None,
    }
    data["tasks"].append(task)
    _save(data)
    return task

def get_next_task() -> Optional[Dict[str, Any]]:
    tasks = _load()["tasks"]
    pending = [t for t in tasks if t.get("status") == "pending"]
    if not pending:
        return None
    return sorted(pending, key=lambda x: (x.get("priority", 999), x.get("created_at", 0)))[0]

def update_task(task_id: str, **fields) -> Optional[Dict[str, Any]]:
    data = _load()
    for t in data["tasks"]:
        if t.get("id") == task_id:
            t.update(fields)
            t["updated_at"] = _now()
            _save(data)
            return t
    return None
