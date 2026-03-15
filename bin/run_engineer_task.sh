#!/bin/bash

TASK=$(python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / "ai-os/runtime/tasks.json"
tasks = json.loads(p.read_text())
print(tasks[0]["goal"])
PY
)

python3 ~/ai-os/agents/engineer/engineer_agent.py "$TASK"
