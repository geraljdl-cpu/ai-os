#!/usr/bin/env bash
# Health check do AI-OS — corre via aios-health-check.timer (cada 5 min)
# Envia alerta Telegram se algum endpoint falhar
set -uo pipefail

source ~/.env.db 2>/dev/null || true
TG_TOKEN="${AIOS_TG_TOKEN:-}"
TG_CHAT="${AIOS_TG_CHAT:-}"
LOG="/home/jdl/ai-os/runtime/logs/health.log"
STATE_FILE="/tmp/aios_health_state"

mkdir -p "$(dirname "$LOG")"

tg_alert() {
    local msg="$1"
    [[ -z "$TG_TOKEN" || -z "$TG_CHAT" ]] && return
    curl -fsS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d chat_id="$TG_CHAT" \
        -d text="$msg" \
        -d parse_mode="Markdown" \
        --max-time 10 > /dev/null 2>&1 || true
}

check() {
    local name="$1" url="$2"
    if curl -fsS --max-time 5 "$url" > /dev/null 2>&1; then
        echo "OK"
    else
        echo "FAIL"
    fi
}

declare -A CHECKS=(
    ["agent-router"]="http://127.0.0.1:5679/health"
    ["agent-core"]="http://127.0.0.1:8010/health"
    ["bash-bridge"]="http://127.0.0.1:8020/health"
    ["ui"]="http://127.0.0.1:3000/"
)

# Ler estado anterior
declare -A PREV_STATE
if [[ -f "$STATE_FILE" ]]; then
    while IFS='=' read -r k v; do
        PREV_STATE["$k"]="$v"
    done < "$STATE_FILE"
fi

FAILED=()
RECOVERED=()
declare -A CUR_STATE

for name in "${!CHECKS[@]}"; do
    url="${CHECKS[$name]}"
    result=$(check "$name" "$url")
    CUR_STATE["$name"]="$result"

    prev="${PREV_STATE[$name]:-OK}"

    if [[ "$result" == "FAIL" && "$prev" == "OK" ]]; then
        FAILED+=("$name")
    elif [[ "$result" == "OK" && "$prev" == "FAIL" ]]; then
        RECOVERED+=("$name")
    fi
done

# Verificar postgres via pg_isready
if docker exec postgres pg_isready -U aios_user -d aios -q 2>/dev/null; then
    CUR_STATE["postgres"]="OK"
    [[ "${PREV_STATE[postgres]:-OK}" == "FAIL" ]] && RECOVERED+=("postgres")
else
    CUR_STATE["postgres"]="FAIL"
    [[ "${PREV_STATE[postgres]:-OK}" == "OK" ]] && FAILED+=("postgres")
fi

# Guardar estado atual
: > "$STATE_FILE"
for k in "${!CUR_STATE[@]}"; do
    echo "$k=${CUR_STATE[$k]}" >> "$STATE_FILE"
done

# Log
TS=$(date -Is)
ALL_OK=true
for k in "${!CUR_STATE[@]}"; do
    [[ "${CUR_STATE[$k]}" == "FAIL" ]] && ALL_OK=false
    echo "[$TS] $k: ${CUR_STATE[$k]}" >> "$LOG"
done

# Alertas
if [[ ${#FAILED[@]} -gt 0 ]]; then
    NAMES=$(printf '%s, ' "${FAILED[@]}" | sed 's/, $//')
    MSG="🔴 *AI-OS ALERTA*: serviço(s) em baixo: \`${NAMES}\`"
    echo "[$TS] ALERT: $MSG" >> "$LOG"
    tg_alert "$MSG"
fi

if [[ ${#RECOVERED[@]} -gt 0 ]]; then
    NAMES=$(printf '%s, ' "${RECOVERED[@]}" | sed 's/, $//')
    MSG="✅ *AI-OS RECOVERED*: \`${NAMES}\` voltou ao ar"
    echo "[$TS] RECOVERED: $MSG" >> "$LOG"
    tg_alert "$MSG"
fi

# Manter log com máx 1000 linhas
tail -1000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"

$ALL_OK && exit 0 || exit 1
