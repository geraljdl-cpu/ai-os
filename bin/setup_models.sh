#!/bin/bash
# setup_models.sh — Instala modelos base no Ollama (nodegpu)
# Usage: ./bin/setup_models.sh [ollama_url]
set -euo pipefail

OLLAMA_URL="${1:-${OLLAMA_URL:-http://192.168.1.120:11434}}"

# Modelos necessários para o agent pipeline
declare -A MODELS=(
  ["qwen2.5-coder:14b"]="coding/engineer (9GB VRAM)"
  ["qwen2.5:14b"]="general/reviewer (9GB VRAM)"
)

# Modelos opcionais (instalar se VRAM disponível)
declare -A OPTIONAL=(
  ["deepseek-r1:14b"]="reasoning/debug (9GB VRAM)"
  ["mistral:7b"]="fast/light tasks (4GB VRAM)"
)

echo "=== setup_models.sh ==="
echo "  OLLAMA_URL=$OLLAMA_URL"
echo ""

_pull() {
  local model="$1"
  local desc="$2"
  echo -n "  Pulling $model ($desc)... "
  resp=$(curl -sf -X POST "$OLLAMA_URL/api/pull" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$model\",\"stream\":false}" \
    --max-time 900 2>/dev/null | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status','?'))" 2>/dev/null || echo "ERRO")
  echo "$resp"
}

_is_installed() {
  local model="$1"
  curl -sf "$OLLAMA_URL/api/tags" 2>/dev/null | \
    python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if '$model' in [m['name'] for m in d.get('models',[])] else 1)" 2>/dev/null
}

echo "Modelos obrigatórios:"
for model in "${!MODELS[@]}"; do
  desc="${MODELS[$model]}"
  if _is_installed "$model"; then
    echo "  [OK]  $model já instalado"
  else
    _pull "$model" "$desc"
  fi
done

echo ""
echo "Modelos opcionais (instalar com: INSTALL_OPTIONAL=1 $0):"
for model in "${!OPTIONAL[@]}"; do
  desc="${OPTIONAL[$model]}"
  if _is_installed "$model"; then
    echo "  [OK]  $model já instalado"
  elif [[ "${INSTALL_OPTIONAL:-0}" == "1" ]]; then
    _pull "$model" "$desc"
  else
    echo "  [--]  $model ($desc) — não instalado"
  fi
done

echo ""
echo "Modelos disponíveis:"
curl -sf "$OLLAMA_URL/api/tags" 2>/dev/null | \
  python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  {m[\"name\"]} ({round(m[\"size\"]/1e9,1)} GB)') for m in d.get('models',[])]"
