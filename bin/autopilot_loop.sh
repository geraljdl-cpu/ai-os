#!/usr/bin/env bash
# autopilot_loop.sh — corre autopilot_worker em loop contínuo.
# Suprime output de "no pending tasks" para não poluir journals.
# O worker tem flock interno; este script tem flock de processo único.

AIOS_ROOT="${AIOS_ROOT:-$HOME/ai-os}"
LOOP_LOCK="$AIOS_ROOT/runtime/autopilot_loop.lock"
SLEEP_IDLE=5    # segundos entre runs quando não há tasks
SLEEP_BUSY=1    # segundos após run com tasks

mkdir -p "$AIOS_ROOT/runtime"

# Garante instância única do loop
exec 8>"$LOOP_LOCK"
if ! flock -n 8; then
    echo "[aios-loop] já em execução — exit"
    exit 0
fi
trap 'flock -u 8; rm -f "$LOOP_LOCK"' EXIT

echo "[aios-loop] iniciado pid=$$  $(date -u +%Y-%m-%dT%H:%M:%SZ)"

while true; do
    # Corre worker; filtra linhas de ruído idle para silenciar journald
    OUTPUT=$(bash "$AIOS_ROOT/bin/autopilot_worker.sh" 2>&1) || true
    FILTERED=$(printf '%s\n' "$OUTPUT" \
        | grep -v "\[autopilot\] no pending tasks" \
        | grep -v "\[autopilot\] mode=.*started=" \
        | grep -v "\[autopilot\] batch stop" \
        || true)

    if [ -n "$FILTERED" ]; then
        printf '%s\n' "$FILTERED"
        SLEEP=$SLEEP_BUSY
    else
        SLEEP=$SLEEP_IDLE
    fi

    sleep "$SLEEP"
done
