#!/usr/bin/env bash
set -euo pipefail

JOBS_DIR="$HOME/ai-os/runtime/jobs"

[ -d "$JOBS_DIR" ] || exit 0

LAST_JOB=$(ls -1t "$JOBS_DIR" 2>/dev/null | head -n1 || true)
[ -z "${LAST_JOB:-}" ] && exit 0

JOB_PATH="$JOBS_DIR/$LAST_JOB"
META="$JOB_PATH/meta.json"

[ -f "$META" ] || exit 0

TEST_RC=$(python3 - <<PY
import json,sys
try:
    print(json.load(open("$META")).get("tests_rc","1"))
except:
    print("1")
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
git merge "$BRANCH" --no-edit || exit 0
git branch -D "$BRANCH" || true
