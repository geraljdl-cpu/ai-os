#!/usr/bin/env bash
# Levanta o stack crítico do AI-OS no boot.
# Usa --pull never para não fazer download de imagens em falta — evita timeout.
# Serviços pesados (ollama, openwebui, n8n, nodered) são iniciados manualmente.
set -euo pipefail

cd /home/jdl/ai-os
export DOCKER_CONFIG=/home/jdl/.docker-systemd

CRITICAL="postgres redis qdrant mosquitto agent-core bash-bridge agent-router"

echo "[aios-stack] a verificar imagens disponíveis..."

TO_START=""
for svc in $CRITICAL; do
    image=$(docker compose config --format json 2>/dev/null \
        | python3 -c "import sys,json; cfg=json.load(sys.stdin); \
          svcs=cfg.get('services',{}); s=svcs.get('$svc',{}); \
          print(s.get('image','') or '')" 2>/dev/null || echo "")

    # agent-router usa build, sempre incluir
    if [[ "$svc" == "agent-router" ]]; then
        TO_START="$TO_START $svc"
        continue
    fi

    if [[ -n "$image" ]] && docker image inspect "$image" &>/dev/null; then
        TO_START="$TO_START $svc"
    else
        echo "[aios-stack] imagem em falta para '$svc' ($image) — a ignorar no boot"
    fi
done

if [[ -z "$TO_START" ]]; then
    echo "[aios-stack] nenhum serviço com imagem disponível — nada a iniciar"
    exit 0
fi

echo "[aios-stack] a iniciar:$TO_START"
docker compose up -d --pull never --no-build $TO_START

echo "[aios-stack] stack crítico UP"
docker compose ps --filter "status=running"
