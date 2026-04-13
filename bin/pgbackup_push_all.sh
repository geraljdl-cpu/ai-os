#!/usr/bin/env bash
# pgbackup_push_all.sh — push backups para Google Drive e node-nas
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/home/jdl/ai-os/backups}"
GDRIVE_REMOTE="jdlaicloud:AI-OS-Backups/pgbackups"
NAS_HOST="192.168.1.203"
NAS_DIR="/cluster-storage/backups/pgbackups"
RCLONE="${HOME}/.local/bin/rclone"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

[ -d "$BACKUP_DIR" ] || { log "BACKUP_DIR_NOT_FOUND=$BACKUP_DIR"; exit 2; }

# ── Google Drive ──────────────────────────────────────────────────────────────
log "Push Google Drive → $GDRIVE_REMOTE"
"$RCLONE" copy "$BACKUP_DIR" "$GDRIVE_REMOTE" \
    --include "aios_db_*.sql.gz" \
    --transfers 2 --checkers 4 \
    --log-level INFO

# retenção cloud: 30 dias
"$RCLONE" delete "$GDRIVE_REMOTE" \
    --include "aios_db_*.sql.gz" \
    --min-age 30d \
    --log-level INFO 2>/dev/null || true

log "Google Drive OK"

# ── node-nas rsync ─────────────────────────────────────────────────────────
log "rsync → ${NAS_HOST}:${NAS_DIR}"
ssh -o BatchMode=yes -o ConnectTimeout=10 jdl@"$NAS_HOST" "mkdir -p $NAS_DIR" 2>/dev/null || { log "WARN: node-nas inacessível, skip"; exit 0; }

rsync -az --delete \
    --include "aios_db_*.sql.gz" \
    --exclude "*" \
    "$BACKUP_DIR/" \
    jdl@"$NAS_HOST":"$NAS_DIR"/

log "node-nas OK"
