from collections import deque
from datetime import datetime, timezone
from typing import Deque, Dict, Any, List

_EVENTS: Deque[Dict[str, Any]] = deque(maxlen=1000)

def emit_event(name: str, payload: dict):
    ev = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "type": name,
        "payload": payload or {},
    }
    _EVENTS.appendleft(ev)
    print(f"EVENT -> {name} | {payload}")

def list_events(limit: int = 50) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    return list(_EVENTS)[:limit]
