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

  # claim next task (Postgres source of truth)
  TASK_LINE=$(python3 - <<'PY' 2>/dev/null
from bin import backlog_pg
t = backlog_pg.get_next_task()
if not t:
    raise SystemExit(2)
print(f"{t.get('id','')}\t{t.get('goal','')}")
PY
  ) || { echo "[autopilot] no pending tasks."; exit 0; }

  TASK_ID="${TASK_LINE%%$'\t'*}"
  GOAL="${TASK_LINE#*$'\t'}"


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

RESPONSE=$(curl -sf -X POST http://127.0.0.1:5679/agent \
  -H "Content-Type: application/json" \
  -H "X-AIOS-TOKEN: ${AIOS_TOKEN:-}" \
  -d "{\"chatInput\": \"$GOAL\", \"mode\": \"openai\"}" 2>&1) || {
  echo "[autopilot] agent unreachable" | tee -a "$EXEC_LOG"
  python3 "$AIOS_ROOT/bin/alerting.py" worker_crash "$JOB_ID" 2>/dev/null || true
  exit 0
}

echo "$RESPONSE" > "$JOBS_DIR/$JOB_ID/agent_response.json"

# Run steps via tools engine
set +e
echo "$RESPONSE" | python3 -c "
import json,sys
r=json.load(sys.stdin)
steps=r.get('steps',[])
print(json.dumps({'steps':steps,'log_path':'$EXEC_LOG'}))
" | AIOS_MODE="$AIOS_MODE" AIOS_ROOT="$AIOS_ROOT" python3 "$AIOS_ROOT/bin/tools_engine.py" | tee -a "$EXEC_LOG"
PIPE_RC=${PIPESTATUS[2]:-1}
set -e

echo "[autopilot] $JOB_ID done" | tee -a "$EXEC_LOG"

python3 - <<PY 2>/dev/null || true
from bin import backlog_pg
backlog_pg.update_task("$TASK_ID", status=("done" if int("$PIPE_RC")==0 else "failed"))
PY

# Alerta se job falhou >3x
ATTEMPTS=$(python3 -c "
import json,os
p=os.path.expanduser('$BACKLOG')
try:
    d=json.load(open(p))
    t=next((t for t in d.get('tasks',[]) if t.get('goal','')=='''$GOAL'''), None)
    print(t.get('attempts',0) if t else 0)
except: print(0)
" 2>/dev/null || echo 0)
if [ "${ATTEMPTS:-0}" -ge 3 ]; then
  python3 "$AIOS_ROOT/bin/alerting.py" job_failed "$JOB_ID" "$ATTEMPTS" 2>/dev/null || true
fi
