#!/usr/bin/env bash
set -euo pipefail

TS="$(date '+%Y-%m-%d_%H-%M-%S')"
OUT="$HOME/ai-os/backups/ai-os_${TS}.tar.gz"

mkdir -p "$HOME/ai-os/backups"

# Backup do código/config; EXCLUI volumes docker e dados pesados (ai-os/data)
tar -czf "$OUT" \
  --ignore-failed-read \
  --warning=no-file-changed \
  --exclude="ai-os/backups" \
  --exclude="ai-os/runtime/state.json" \
  --exclude="ai-os/data" \
  -C "$HOME" "ai-os"

echo "OK: $OUT"

echo "Uploading to cloud..."
rclone copy "$OUT" jdlaicloud:AI-OS-Backups --progress
echo "Cloud upload finished"
