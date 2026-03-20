#!/bin/bash
# insurance_daily.sh — Job diário: scrape + alertas
# Corre às 07:30 via aios-insurance-alerts.timer

set -euo pipefail
cd /home/jdl/ai-os

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

log "=== insurance_daily START ==="

# 1. Scraper portal particular
log "Scraper particular..."
python3 bin/fidelidade_scraper.py --safe 2>&1 || log "WARN: scraper particular falhou"

# 2. Scraper portal empresa
log "Scraper empresa..."
python3 bin/fidelidade_scraper.py --empresa --safe 2>&1 || log "WARN: scraper empresa falhou"

# 3. Alertas + Telegram + email
log "Alertas..."
python3 bin/insurance_alerts.py 2>&1

log "=== insurance_daily END ==="
