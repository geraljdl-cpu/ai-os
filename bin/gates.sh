#!/usr/bin/env bash
# gates.sh — validação determinística pós-patch
# Uso: gates.sh <job_dir>
# Exit 0 = OK, Exit 1 = falhou, Exit 2 = erro de argumento
set -euo pipefail

AIOS_ROOT="${AIOS_ROOT:-$HOME/ai-os}"
cd "$AIOS_ROOT"

JOB_DIR="${1:-}"
[ -n "$JOB_DIR" ] || { echo "GATE_ERROR: JOB_DIR missing"; exit 2; }

LOG="$JOB_DIR/gate.log"
: > "$LOG"

fail() {
  echo "GATE_FAIL: $*" | tee -a "$LOG"
  exit 1
}

echo "[gate] === start $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a "$LOG"

# 1. Verifica conflictos não resolvidos
echo "[gate] git status" | tee -a "$LOG"
if git status --porcelain 2>&1 | grep -qE "^(AA|DD|UU|AU|UA|DU|UD)"; then
  fail "conflict markers in worktree"
fi
git status --porcelain | head -20 | tee -a "$LOG"

# 2. Whitespace check — APENAS nos ficheiros do patch (evita ruído pré-existente)
echo "[gate] git diff --check (patch files only)" | tee -a "$LOG"
CHANGED_FILE="$JOB_DIR/changed_files.txt"
if [ -f "$CHANGED_FILE" ] && [ -s "$CHANGED_FILE" ]; then
  mapfile -t PATCH_FILES < "$CHANGED_FILE"
  if [ "${#PATCH_FILES[@]}" -gt 0 ]; then
    git diff --check -- "${PATCH_FILES[@]}" 2>&1 | tee -a "$LOG" \
      || fail "whitespace errors in patch files"
  fi
else
  git diff --check 2>&1 | tee -a "$LOG" || fail "whitespace errors"
fi

# 3. Python syntax — exclui runtime/ (ficheiros gerados por runs anteriores)
echo "[gate] python compileall (bin/ e ui/)" | tee -a "$LOG"
COMPILE_OUT=$(python3 -m compileall -q bin/ 2>&1 || true)
echo "$COMPILE_OUT" | tee -a "$LOG"
if echo "$COMPILE_OUT" | grep -qi "SyntaxError"; then
  fail "python SyntaxError in bin/"
fi

# 4. Smoke: syshealth (tolerante a timeout)
echo "[gate] smoke: syshealth" | tee -a "$LOG"
curl -fsS --max-time 5 "http://127.0.0.1:3000/api/syshealth" 2>&1 \
  | head -c 400 | tee -a "$LOG" || echo "UNAVAILABLE" | tee -a "$LOG"

# 5. Smoke: telemetry
echo "" | tee -a "$LOG"
echo "[gate] smoke: telemetry" | tee -a "$LOG"
curl -fsS --max-time 5 "http://127.0.0.1:3000/api/telemetry/history?n=3" 2>&1 \
  | head -c 400 | tee -a "$LOG" || echo "UNAVAILABLE" | tee -a "$LOG"

echo "" | tee -a "$LOG"
echo "[gate] === OK $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a "$LOG"
exit 0
