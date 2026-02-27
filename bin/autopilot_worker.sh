#!/usr/bin/env bash
set -euo pipefail

BACKLOG="$HOME/ai-os/runtime/backlog.json"

GOAL=$(python3 - <<PY
import json
p="$BACKLOG"
try:
    data=json.load(open(p))
    tasks=data.get("tasks",[])
    if tasks:
        goal=tasks[0]["goal"]
        data["tasks"]=tasks[1:]
        json.dump(data,open(p,"w"),indent=2)
        print(goal)
    else:
        print("Responder apenas IDLE")
except:
    print("Responder apenas IDLE")
PY
)

echo "Running goal: $GOAL"

# auth token (read from agent-router container)
TOKEN=$(docker exec agent-router sh -lc 'echo -n $AIOS_TOKEN' 2>/dev/null || true)
[ -z "${TOKEN:-}" ] && echo "WARN: AIOS_TOKEN missing; autopilot call may fail"

curl -sS -X POST http://localhost:5679/autopilot \
  -H "Content-Type: application/json" \
  -H "X-AIOS-TOKEN: $TOKEN" \
  -d "{\"goal\":\"$GOAL\"}" >/dev/null || true

# -------- AUTO MERGE --------
JOBS_DIR="$HOME/ai-os/runtime/jobs"
[ -d "$JOBS_DIR" ] || exit 0

LAST_JOB=$(ls -1t "$JOBS_DIR" 2>/dev/null | head -n1 || true)
[ -z "${LAST_JOB:-}" ] && exit 0

META="$JOBS_DIR/$LAST_JOB/meta.json"
[ -f "$META" ] || exit 0

TEST_RC=$(python3 - <<PY
import json
print(json.load(open("$META")).get("tests_rc","1"))
PY
)

[ "$TEST_RC" != "0" ] && exit 0

BRANCH=$(python3 - <<PY
import json
print(json.load(open("$META")).get("branch",""))
PY
)

[ -z "$BRANCH" ] && exit 0

echo "Auto-merging $BRANCH"

cd ~/ai-os
git checkout main
git pull || true
git merge "$BRANCH" --no-edit || true
git branch -D "$BRANCH" || true
