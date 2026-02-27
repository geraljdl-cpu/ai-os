#!/usr/bin/env bash
set -euo pipefail

AIOS_ROOT="${AIOS_ROOT:-$HOME/ai-os}"
BACKLOG="$AIOS_ROOT/runtime/backlog.json"
JOBS_DIR="$AIOS_ROOT/runtime/jobs"
LOCK_FILE="$AIOS_ROOT/runtime/autopilot.lock"
AIOS_MODE="${AIOS_MODE:-simulate}"

mkdir -p "$JOBS_DIR" "$AIOS_ROOT/runtime"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[autopilot] already running. Exiting."
  exit 0
fi
trap 'flock -u 9 || true; rm -f "$LOCK_FILE"' EXIT

echo "[autopilot] mode=$AIOS_MODE started=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

GOAL=$(python3 -c "
import json,sys,os
p=os.path.expanduser('$BACKLOG')
if not os.path.exists(p): sys.exit(2)
d=json.load(open(p))
tasks=d.get('tasks',[])
pending=[t for t in tasks if t.get('status','pending')=='pending']
if not pending: sys.exit(2)
print(pending[0].get('goal',''))
" 2>/dev/null) || { echo "[autopilot] no pending tasks."; exit 0; }

JOB_ID="job_$(date +%s)"
mkdir -p "$JOBS_DIR/$JOB_ID"
EXEC_LOG="$JOBS_DIR/$JOB_ID/exec.log"

echo "[autopilot] job=$JOB_ID goal=$GOAL"
echo "JOB=$JOB_ID GOAL=$GOAL MODE=$AIOS_MODE" > "$EXEC_LOG"

DENIED=("rm -rf" "dd " "mkfs" "shutdown" "reboot" "sudo" "chmod 777")

is_allowed(){
  local cmd="$1"
  for p in "${DENIED[@]}"; do
    if echo "$cmd" | grep -qF "$p"; then
      echo "[BLOCKED] $p" | tee -a "$EXEC_LOG"
      return 1
    fi
  done
  return 0
}

RESPONSE=$(curl -sf -X POST http://localhost:7070/agent \
  -H "Content-Type: application/json" \
  -d "{\"goal\": \"$GOAL\"}" 2>&1) || {
  echo "[autopilot] agent unreachable" | tee -a "$EXEC_LOG"
  exit 0
}

echo "$RESPONSE" > "$JOBS_DIR/$JOB_ID/agent_response.json"

STEPS=$(echo "$RESPONSE" | python3 -c "
import json,sys
r=json.load(sys.stdin)
steps=r.get('steps',r.get('response',{}).get('steps',[]))
for s in steps:
    inp=s.get('input',{})
    cmd=inp.get('cmd','') if isinstance(inp,dict) else s.get('cmd','')
    if cmd: print(cmd)
" 2>/dev/null)

while IFS= read -r cmd; do
  [[ -z "$cmd" ]] && continue
  echo "[step] $cmd" | tee -a "$EXEC_LOG"
  if ! is_allowed "$cmd"; then continue; fi
  if [[ "$AIOS_MODE" == "simulate" ]]; then
    echo "[SIMULATE] $cmd" | tee -a "$EXEC_LOG"
  else
    out=$(cd "$AIOS_ROOT" && eval "$cmd" 2>&1) && code=$? || code=$?
    echo "[exit=$code] $out" | tee -a "$EXEC_LOG"
  fi
done <<< "$STEPS"

echo "[autopilot] $JOB_ID done" | tee -a "$EXEC_LOG"
