#!/usr/bin/env bash
# pgbackup_push_cloud.sh — push pg_dump .sql.gz para cloud, com retenção
# Corre às 03:15 via aios-pgbackup-push.timer (depois do backup das 03:00)
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/home/jdl/ai-os/backups}"
REMOTE="${REMOTE:-jdlaicloud:AI-OS-Backups/pgbackups}"

[ -d "$BACKUP_DIR" ] || { echo "BACKUP_DIR_NOT_FOUND=$BACKUP_DIR"; exit 2; }

echo "[push] $(date -u +%Y-%m-%dT%H:%M:%SZ) backup_dir=$BACKUP_DIR remote=$REMOTE"

# cria destino se não existir
rclone mkdir "$REMOTE" 2>/dev/null || true

# push apenas os sql.gz (exclui tar.gz e outros)
rclone copy "$BACKUP_DIR" "$REMOTE" \
  --include "aios_db_*.sql.gz" \
  --transfers 2 --checkers 4 \
  --log-level INFO

# retenção cloud: apaga ficheiros com mais de 30 dias
rclone delete "$REMOTE" \
  --include "aios_db_*.sql.gz" \
  --min-age 30d \
  --log-level INFO 2>/dev/null || true

echo "[push] done"
