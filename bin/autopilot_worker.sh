#!/usr/bin/env bash
set -euo pipefail

ROOT="$HOME/ai-os"
BACKLOG="$ROOT/runtime/backlog.json"
JOBS_DIR="$ROOT/runtime/jobs"
AR_URL="http://127.0.0.1:5679"

mkdir -p "$JOBS_DIR"

GOAL="$(python3 - <<'PY_INNER'
import json, os, sys
p=os.path.expanduser("~/ai-os/runtime/backlog.json")
d=json.load(open(p)) if os.path.exists(p) else {"tasks":[]}
tasks=d.get("tasks",[])
if not tasks:
    print("")
    sys.exit(0)
goal=(tasks[0].get("goal","") or "").strip()
d["tasks"]=tasks[1:]
json.dump(d, open(p,"w"), indent=2, ensure_ascii=False)
print(goal)
PY_INNER
)"

if [ -z "${GOAL:-}" ]; then
  echo "Running goal: IDLE"
  exit 0
fi

echo "Running goal: $GOAL"

JOB_ID="$(date +%Y%m%d_%H%M%S)_$(openssl rand -hex 4)"
JOB_PATH="$JOBS_DIR/$JOB_ID"
mkdir -p "$JOB_PATH"

TOKEN="$(docker exec agent-router sh -lc 'echo -n $AIOS_TOKEN' 2>/dev/null || true)"
if [ -z "${TOKEN:-}" ]; then
  echo "ERROR: AIOS_TOKEN missing" > "$JOB_PATH/log.txt"
  exit 1
fi

REQ_JSON="$(python3 - <<PY_INNER
import json
print(json.dumps({"chatInput": """$GOAL""", "mode":"openai"}, ensure_ascii=False))
PY_INNER
)"

RESP="$(curl -sS -X POST "$AR_URL/agent"   -H "Content-Type: application/json"   -H "X-AIOS-TOKEN: $TOKEN"   -d "$REQ_JSON" || true)"

echo "$RESP" > "$JOB_PATH/agent_response.json"

# Execute steps locally on HOST
python3 - "$ROOT" "$JOB_PATH" <<'PY_INNER'
import json, sys, pathlib, subprocess, shlex

root = pathlib.Path(sys.argv[1])
job_path = pathlib.Path(sys.argv[2])
j = json.loads((job_path/"agent_response.json").read_text(encoding="utf-8"))
steps = j.get("steps") or []

lines = []
for i, st in enumerate(steps, start=1):
    tool = st.get("tool")
    if tool != "bash":
        lines.append(f"SKIP step{i}: tool={tool}")
        continue
    cmd = (st.get("input") or {}).get("cmd","").strip()
    if not cmd:
        lines.append(f"SKIP step{i}: empty cmd")
        continue

    # run inside repo root
    try:
        p = subprocess.run(["bash","-lc",cmd], cwd=str(root), capture_output=True, text=True)
        lines.append(f"RUN step{i}: {cmd}")
        lines.append(f"code={p.returncode}")
        if p.stdout: lines.append(p.stdout.rstrip())
        if p.stderr: lines.append(p.stderr.rstrip())
    except Exception as e:
        lines.append(f"FAIL step{i}: {cmd} :: {e}")

(job_path/"exec.log.txt").write_text("\n".join(lines)+"\n", encoding="utf-8")
PY_INNER

echo "JOB_ID=$JOB_ID" > "$JOB_PATH/log.txt"
echo "DONE"
