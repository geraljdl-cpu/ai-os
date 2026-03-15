#!/usr/bin/env bash
# radar_run_all.sh — Full radar pipeline
# Runs: fetch → normalize → score → bridge for all active sources
# Usage: bash bin/radar_run_all.sh [--source base|ted|dr|all]

set -euo pipefail

AIOS_ROOT="/home/jdl/ai-os"
LOG_DIR="$AIOS_ROOT/runtime/radar/logs"
mkdir -p "$LOG_DIR"

LOG="$LOG_DIR/radar_$(date +%Y%m%d_%H%M%S).log"
SOURCE="${1:-all}"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

run_source() {
    local src="$1"
    log "── SOURCE: $src ──────────────────────────────"

    log "  [1/4] collect $src"
    python3 "$AIOS_ROOT/bin/radar_${src}.py" 2>&1 | tee -a "$LOG"

    log "  [2/4] normalize --source $src"
    python3 "$AIOS_ROOT/bin/radar_normalize.py" --source "$src" 2>&1 | tee -a "$LOG"

    log "  [3/4] score --source $src"
    python3 "$AIOS_ROOT/bin/radar_score.py" --source "$src" 2>&1 | tee -a "$LOG"

    log "  [4/4] twin_bridge --source $src"
    python3 "$AIOS_ROOT/bin/radar_twin_bridge.py" --source "$src" 2>&1 | tee -a "$LOG"

    log "  done: $src"
}

log "=== RADAR RUN ALL  source=${SOURCE} ==="

if [ "$SOURCE" = "all" ]; then
    run_source "base"
    run_source "ted"
    run_source "dr"
elif [ "$SOURCE" = "base" ] || [ "$SOURCE" = "ted" ] || [ "$SOURCE" = "dr" ]; then
    run_source "$SOURCE"
else
    echo "Usage: $0 [base|ted|dr|all]" >&2
    exit 1
fi

log "=== DONE ==="
