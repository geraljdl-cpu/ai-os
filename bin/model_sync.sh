#!/bin/bash
# model_sync.sh — Sincroniza modelos NAS → armazenamento activo (nodegpu)
# Assumes: NAS montado em /cluster, modelos em /cluster/models_archive/
# Usage: ./bin/model_sync.sh [model_name]
#        ./bin/model_sync.sh qwen2.5-coder:14b
set -euo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://192.168.1.120:11434}"
NAS_ARCHIVE="${NAS_ARCHIVE:-/cluster/models_archive}"
FAST_MODELS="${FAST_MODELS:-/fast/models}"

echo "=== model_sync.sh ==="
echo "  NAS_ARCHIVE=$NAS_ARCHIVE"
echo "  FAST_MODELS=$FAST_MODELS"
echo "  OLLAMA_URL=$OLLAMA_URL"
echo ""

# Verificar NAS acessível
if [ ! -d "$NAS_ARCHIVE" ]; then
  echo "[WARN] NAS não montado em $NAS_ARCHIVE"
  echo "       Montar com: sudo mount 192.168.1.118:/models $NAS_ARCHIVE"
  echo "       Nada a sincronizar."
  exit 0
fi

TARGET="$1"

# Listar modelos disponíveis no arquivo NAS
echo "Modelos no arquivo NAS ($NAS_ARCHIVE):"
if ls "$NAS_ARCHIVE"/*.gguf "$NAS_ARCHIVE"/*.bin 2>/dev/null | head -10; then
  :
else
  # Tentar formato de manifests Ollama
  if [ -d "$NAS_ARCHIVE/manifests" ]; then
    find "$NAS_ARCHIVE/manifests" -type f | sed 's|.*/||' | head -10
  else
    echo "  (nenhum modelo encontrado em $NAS_ARCHIVE)"
    exit 0
  fi
fi

echo ""

# Copiar modelos de NFS → FAST se disponível
if [ -n "$TARGET" ] && [ -d "$NAS_ARCHIVE/blobs" ]; then
  echo "A copiar $TARGET de NAS para $FAST_MODELS..."
  mkdir -p "$FAST_MODELS"
  # Localizar blobs do modelo no arquivo
  MODEL_DIR="$NAS_ARCHIVE/manifests/registry.ollama.ai/library/${TARGET%%:*}"
  if [ -d "$MODEL_DIR" ]; then
    echo "  A sincronizar manifests..."
    rsync -av --progress "$MODEL_DIR/" "$FAST_MODELS/manifests/registry.ollama.ai/library/${TARGET%%:*}/" 2>/dev/null \
      || cp -rv "$MODEL_DIR" "$FAST_MODELS/manifests/registry.ollama.ai/library/" 2>/dev/null \
      || echo "  [WARN] rsync/cp falhou — verificar permissões"
    echo "  Após cópia, reiniciar Ollama para reconhecer modelos."
  else
    echo "  Modelo $TARGET não encontrado no arquivo NAS."
  fi
else
  echo "Para sincronizar um modelo específico: $0 <model_name>"
  echo "Exemplo: $0 qwen2.5-coder:14b"
fi

echo ""
echo "Modelos actualmente no Ollama:"
curl -sf "$OLLAMA_URL/api/tags" 2>/dev/null | \
  python3 -c "import sys,json; d=json.load(sys.stdin); [print(f'  {m[\"name\"]} ({round(m[\"size\"]/1e9,1)} GB)') for m in d.get('models',[])]" \
  || echo "  (Ollama inacessível)"
