#!/usr/bin/env bash
set -euo pipefail

AIOS_ROOT="${AIOS_ROOT:-$HOME/ai-os}"
BACKLOG="$AIOS_ROOT/runtime/backlog.json"
JOBS_DIR="$AIOS_ROOT/runtime/jobs"
LOCK_FILE="$AIOS_ROOT/runtime/autopilot.lock"
AIOS_MODE="${AIOS_MODE:-openai}"

mkdir -p "$JOBS_DIR" "$AIOS_ROOT/runtime"

cd "$AIOS_ROOT"

cd "$AIOS_ROOT"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "[autopilot] already running. Exiting."
  exit 0
fi
trap 'flock -u 9 || true; rm -f "$LOCK_FILE"' EXIT

echo "[autopilot] mode=$AIOS_MODE started=$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# pre-init log (avoid unbound EXEC_LOG on early exits)
EARLY_JOB_ID="early_$(date +%s)"
mkdir -p "$JOBS_DIR/$EARLY_JOB_ID"
EXEC_LOG="$JOBS_DIR/$EARLY_JOB_ID/exec.log"
touch "$EXEC_LOG"

# pre-init log (avoid unbound EXEC_LOG on early exits)
EARLY_JOB_ID="early_$(date +%s)"
mkdir -p "$JOBS_DIR/$EARLY_JOB_ID"
EXEC_LOG="$JOBS_DIR/$EARLY_JOB_ID/exec.log"
touch "$EXEC_LOG"

  
# --- BATCH_LOOP_BEGIN ---
MAX_TASKS="${AIOS_MAX_TASKS:-5}"
MAX_SECS="${AIOS_MAX_SECS:-45}"
START_TS=$(date +%s)
COUNT=0

while true; do
  # stop conditions
  NOW_TS=$(date +%s)
  ELAPSED=$((NOW_TS-START_TS))
  if [ "$COUNT" -ge "$MAX_TASKS" ] || [ "$ELAPSED" -ge "$MAX_SECS" ]; then
    echo "[autopilot] batch stop count=$COUNT elapsed=${ELAPSED}s"
    break
  fi

# claim next task (Postgres source of truth)
  TASK_LINE=$(python3 -c 'import json; from bin import backlog_pg; t=backlog_pg.peek_next_task_json(); print("" if not t else json.dumps(t, ensure_ascii=False))')
  echo "[autopilot] TASK_LINE=$TASK_LINE" >> "$EXEC_LOG"
  if [ -z "${TASK_LINE:-}" ]; then
    echo "[autopilot] no pending tasks."
    break
  fi
  # parse TASK_LINE json -> TASK_ID + GOAL (tab-separated, robust)
  TG=$(python3 -c 'import json,sys; t=json.loads(sys.argv[1]); print((t.get("id",""))+"\t"+(t.get("goal","")) )' "$TASK_LINE")
  TASK_ID=$(printf "%s" "$TG" | cut -f1)
  GOAL=$(printf "%s" "$TG" | cut -f2-)
  if [ -z "${GOAL:-}" ]; then
    echo "[autopilot] ERROR empty GOAL; refusing to run task_id=$TASK_ID" | tee -a "$EXEC_LOG"
    python3 - <<PY_FAIL 2>/dev/null || true
from bin import backlog_pg
backlog_pg.update_task("$TASK_ID", status="failed", last_error="empty goal")
PY_FAIL
    break
  fi
  GOAL=$(python3 -c 'import sys,re; s=sys.stdin.read().replace("\n"," "); print(re.sub(r"\s+"," ",s).strip())' <<<"$GOAL")
  # sanitize goal (avoid breaking JSON payload)

  PROMPT="DEV_TASK: $GOAL
REGRAS:
- Responde APENAS em JSON no formato do agent-router.
- Preenche steps com tool calls (bash/git/file edits) para implementar a tarefa.
- Não escrevas texto fora do JSON."

  # sanitize goal (avoid breaking JSON payload)


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

    # auth token (read from agent-router container; avoids systemd/env issues)
    # auth token (read from agent-router container; avoids systemd/env issues)
    PAYLOAD=$(python3 -c 'import json,sys; print(json.dumps({"chatInput": sys.argv[1], "mode": sys.argv[2], "ai": True}))' "$PROMPT" "$AIOS_MODE")
  RESPONSE=$(curl -sS -X POST http://127.0.0.1:5679/agent \
      -H "Content-Type: application/json" \
    -H "X-AIOS-TOKEN: ${AIOS_TOKEN:-}" \
    -d "$PAYLOAD" 2>&1) || {
    echo "[autopilot] agent unreachable" | tee -a "$EXEC_LOG"
    python3 "$AIOS_ROOT/bin/alerting.py" worker_crash "$JOB_ID" 2>/dev/null || true
    exit 0
  }

echo "$RESPONSE" > "$JOBS_DIR/$JOB_ID/agent_response.json"

# --- requeue on transient agent error ---
STATUS=$(python3 - <<'PY2'
import json,sys
try:
    r=json.load(sys.stdin)
    print(r.get("status",""))
except:
    print("")
PY2
<<<"$RESPONSE")

if [ "$STATUS" = "error" ]; then
  echo "[autopilot] agent returned status=error; requeue task" | tee -a "$EXEC_LOG"
  python3 - <<PY3 2>/dev/null || true
from bin import backlog_pg
backlog_pg.update_task("$TASK_ID", status="pending", last_error="agent status=error")
PY3
  exit 0
fi
# --- end requeue block ---

# Run steps via tools engine — captura output para success gate
GATE_FILE="$JOBS_DIR/$JOB_ID/gate_result.json"
set +e
echo "$RESPONSE" | python3 -c "
import json,sys
r=json.load(sys.stdin)
steps=r.get('steps',[])
print(json.dumps({'steps':steps,'log_path':'$EXEC_LOG'}))
" | AIOS_MODE="$AIOS_MODE" AIOS_ROOT="$AIOS_ROOT" python3 "$AIOS_ROOT/bin/tools_engine.py" \
  | tee -a "$EXEC_LOG" > "$GATE_FILE"
PIPE_RC=${PIPESTATUS[2]:-1}
set -e

# ── Success gate ──────────────────────────────────────────────────────────────
# Analisa JSON de resultados: ok:false ou blocked em qualquer step → failed.
# Log lines do tools_engine começam com [YYYY-MM-DDTHH; filtra para isolar JSON.
GATE_OUT=$(python3 - <<PYGATE 2>/dev/null
import json, sys
gate_file = "$GATE_FILE"
try:
    with open(gate_file) as f:
        content = f.read()
    json_text = "\n".join(l for l in content.splitlines() if not l.startswith("["))
    data = json.loads(json_text)
except Exception as e:
    print("failed\t0\ttools_engine output inválido: " + str(e)[:80])
    sys.exit()
results = data.get("results", [])
if not results:
    print("done\t-1\t")
    sys.exit()
for i, r in enumerate(results):
    res = r.get("result", {})
    blocked = res.get("blocked")
    ok = res.get("ok", True)
    if blocked:
        print("failed\t" + str(i) + "\tblocked: " + str(blocked)[:120])
        sys.exit()
    if not ok:
        err = (res.get("error") or res.get("stderr") or "unknown")[:120]
        print("failed\t" + str(i) + "\tok=false: " + err)
        sys.exit()
print("done\t-1\t")
PYGATE
)

FINAL_STATUS=$(printf '%s' "$GATE_OUT" | cut -f1)
GATE_STEP=$(printf '%s'   "$GATE_OUT" | cut -f2)
GATE_REASON=$(printf '%s' "$GATE_OUT" | cut -f3-)

if [ "${FINAL_STATUS:-failed}" = "done" ]; then
  echo "[autopilot] SUCCESS all steps ok" | tee -a "$EXEC_LOG"
else
  echo "[autopilot] FAILED step=$GATE_STEP reason=$GATE_REASON" | tee -a "$EXEC_LOG"
fi

# Actualiza Postgres com status real e last_error
TASK_FINAL_STATUS="$FINAL_STATUS" TASK_FINAL_ERROR="$GATE_REASON" TASK_ID_ENV="$TASK_ID" \
python3 - <<'PY' 2>/dev/null || true
import os
from bin import backlog_pg
status  = os.environ.get("TASK_FINAL_STATUS", "failed")
error   = os.environ.get("TASK_FINAL_ERROR", "") if status == "failed" else None
task_id = os.environ.get("TASK_ID_ENV", "")
if task_id:
    backlog_pg.update_task(task_id, status=status, last_error=error or None)
PY

echo "[autopilot] $JOB_ID done status=${FINAL_STATUS:-failed}" | tee -a "$EXEC_LOG"

  COUNT=$((COUNT+1))
  continue


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
# ensure token when running via systemd
if [ -z "$AIOS_TOKEN" ]; then
  AIOS_TOKEN=$(docker exec agent-router sh -lc 'echo -n $AIOS_TOKEN' 2>/dev/null || true)
fi

done
# --- BATCH_LOOP_END ---

