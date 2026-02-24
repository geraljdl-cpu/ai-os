#!/bin/bash
BACKUP_FILE="/home/jdl/ai-os_AUTO_BACKUP.tar.gz"
LOG="/home/jdl/cron_backup.log"

echo "$(date) - Iniciando backup..." >> $LOG

/usr/bin/tar -czf $BACKUP_FILE \
  --exclude='/home/jdl/ai-os/data/ollama' \
  --exclude='/home/jdl/ai-os/data/postgres' \
  --exclude='/home/jdl/ai-os/data/grafana' \
  --exclude='/home/jdl/ai-os/data/redis' \
  /home/jdl/ai-os 2>/dev/null

/usr/bin/rclone copy $BACKUP_FILE jdlaicloud:AI-OS-Backups >> $LOG 2>&1

echo "$(date) - Backup concluído: $(ls -lh $BACKUP_FILE | awk '{print $5}')" >> $LOG
