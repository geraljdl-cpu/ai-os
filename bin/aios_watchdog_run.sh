#!/usr/bin/env bash
# AIOS Watchdog — healthcheck + auto-recover
# Runs every 60s via aios-watchdog.timer
set -euo pipefail

AIOS_ROOT="/home/jdl/ai-os"
ENV_FILE="$HOME/.env.db"
LOG_PREFIX="[watchdog $(date '+%H:%M:%S')]"

# Load Telegram creds if available
TG_TOKEN=""; TG_CHAT=""
if [ -f "$ENV_FILE" ]; then
  TG_TOKEN=$(grep '^AIOS_TG_TOKEN=' "$ENV_FILE" | cut -d= -f2-)
  TG_CHAT=$(grep '^AIOS_TG_CHAT='  "$ENV_FILE" | cut -d= -f2-)
fi

tg_alert() {
  local msg="$1"
  if [ -n "$TG_TOKEN" ] && [ -n "$TG_CHAT" ]; then
    curl -sS -m 8 "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
      -d chat_id="$TG_CHAT" -d text="$msg" -d parse_mode="Markdown" >/dev/null 2>&1 || true
  fi
}

recovered=()

# ── 1. agent-router ────────────────────────────────────────────
if ! curl -fsS -m 5 http://127.0.0.1:5679/health >/dev/null 2>&1; then
  echo "$LOG_PREFIX agent-router DOWN — restart"
  docker restart agent-router >/dev/null 2>&1 || true
  sleep 5
  if curl -fsS -m 5 http://127.0.0.1:5679/health >/dev/null 2>&1; then
    echo "$LOG_PREFIX agent-router RECOVERED"
    recovered+=("agent-router")
  else
    echo "$LOG_PREFIX agent-router STILL DOWN"
    tg_alert "🔴 *AIOS Watchdog*: agent-router continua DOWN após restart"
  fi
else
  echo "$LOG_PREFIX agent-router OK"
fi

# ── 2. agent-core ──────────────────────────────────────────────
if ! curl -fsS -m 5 http://127.0.0.1:8010/health >/dev/null 2>&1; then
  echo "$LOG_PREFIX agent-core DOWN — restart"
  docker restart agent-core >/dev/null 2>&1 || true
  sleep 5
  if curl -fsS -m 5 http://127.0.0.1:8010/health >/dev/null 2>&1; then
    echo "$LOG_PREFIX agent-core RECOVERED"
    recovered+=("agent-core")
  else
    echo "$LOG_PREFIX agent-core STILL DOWN"
    tg_alert "🔴 *AIOS Watchdog*: agent-core continua DOWN após restart"
  fi
else
  echo "$LOG_PREFIX agent-core OK"
fi

# ── 3. postgres ────────────────────────────────────────────────
if ! docker exec postgres pg_isready -U aios_user -d aios -q >/dev/null 2>&1; then
  echo "$LOG_PREFIX postgres DOWN — restart"
  docker restart postgres >/dev/null 2>&1 || true
  sleep 8
  if docker exec postgres pg_isready -U aios_user -d aios -q >/dev/null 2>&1; then
    echo "$LOG_PREFIX postgres RECOVERED"
    recovered+=("postgres")
  else
    echo "$LOG_PREFIX postgres STILL DOWN"
    tg_alert "🔴 *AIOS Watchdog*: postgres continua DOWN após restart"
  fi
else
  echo "$LOG_PREFIX postgres OK"
fi

# ── 4. aios-ui ─────────────────────────────────────────────────
if ! curl -fsS -m 5 http://127.0.0.1:3000/health >/dev/null 2>&1; then
  if ! systemctl is-active --quiet aios-ui.service; then
    echo "$LOG_PREFIX aios-ui DOWN — restart"
    sudo systemctl restart aios-ui.service >/dev/null 2>&1 || true
    sleep 3
    if systemctl is-active --quiet aios-ui.service; then
      echo "$LOG_PREFIX aios-ui RECOVERED"
      recovered+=("aios-ui")
    else
      echo "$LOG_PREFIX aios-ui STILL DOWN"
      tg_alert "🔴 *AIOS Watchdog*: aios-ui continua DOWN após restart"
    fi
  fi
else
  echo "$LOG_PREFIX aios-ui OK"
fi

# ── Alert on recoveries ────────────────────────────────────────
if [ ${#recovered[@]} -gt 0 ]; then
  svcs=$(IFS=', '; echo "${recovered[*]}")
  tg_alert "✅ *AIOS Watchdog*: recuperados → $svcs"
fi

echo "$LOG_PREFIX done"
