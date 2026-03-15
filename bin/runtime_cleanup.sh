#!/usr/bin/env bash
# Rotação do runtime do AI-OS
# Corre via aios-runtime-cleanup.timer (diário às 02:00)
set -euo pipefail

RUNTIME="/home/jdl/ai-os/runtime"
LOG="$RUNTIME/logs/cleanup.log"
KEEP_DAYS=7

mkdir -p "$RUNTIME/logs"
echo "[$(date -Is)] cleanup iniciado" >> "$LOG"

# --- 1. Jobs antigos (subdiretorios com nome timestamp) ---
JOBS_BEFORE=$(find "$RUNTIME/jobs" -mindepth 1 -maxdepth 1 | wc -l)
find "$RUNTIME/jobs" -mindepth 1 -maxdepth 1 -mtime +$KEEP_DAYS -exec rm -rf {} +
JOBS_AFTER=$(find "$RUNTIME/jobs" -mindepth 1 -maxdepth 1 | wc -l)
echo "[$(date -Is)] jobs: $JOBS_BEFORE → $JOBS_AFTER (removidos $((JOBS_BEFORE - JOBS_AFTER)))" >> "$LOG"

# --- 2. Ficheiros temp na raiz do runtime (>7 dias, exceto configs ativos) ---
KEEP_PATTERN="-not -name config.json \
              -not -name dmx_state.json \
              -not -name model_override.json \
              -not -name worker.last_seen \
              -not -name autopilot_version.txt \
              -not -name aios.env"

TEMP_COUNT=0
while IFS= read -r -d '' f; do
    rm -f "$f"
    TEMP_COUNT=$((TEMP_COUNT + 1))
done < <(find "$RUNTIME" -maxdepth 1 -type f -mtime +$KEEP_DAYS \
           -not -name "config.json" \
           -not -name "dmx_state.json" \
           -not -name "model_override.json" \
           -not -name "worker.last_seen" \
           -not -name "autopilot_version.txt" \
           -not -name "aios.env" \
           -print0)
echo "[$(date -Is)] temp root: removidos $TEMP_COUNT ficheiros" >> "$LOG"

# --- 3. Logs antigos ---
find "$RUNTIME/logs" -type f -name "*.log" -mtime +30 -delete
find "$RUNTIME/autopilot" -type f -mtime +$KEEP_DAYS -delete 2>/dev/null || true

# --- 4. Tamanho final ---
SIZE=$(du -sh "$RUNTIME" 2>/dev/null | cut -f1)
echo "[$(date -Is)] cleanup concluído — runtime: $SIZE" >> "$LOG"

# manter só últimas 500 linhas do log
tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
