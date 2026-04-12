#!/bin/bash
# validate_local_agent.sh — Valida stack local de coding agent
# Verifica: Ollama acessível, modelos presentes, Aider instalado, pipeline OK
set -euo pipefail

OLLAMA_URL="${OLLAMA_URL:-http://192.168.1.202:11434}"
REQUIRED_MODELS=("qwen2.5-coder:14b" "qwen2.5:14b")
AIDER_BIN="${AIDER_BIN:-aider}"
AIOS_ROOT="${AIOS_ROOT:-$HOME/ai-os}"
PASS=0; FAIL=0

ok()   { echo "  [OK]  $*"; PASS=$((PASS+1)); }
fail() { echo "  [FAIL] $*"; FAIL=$((FAIL+1)); }
info() { echo "  [--]  $*"; }

echo "=== AI-OS Local Agent Validation ==="
echo "  OLLAMA_URL=$OLLAMA_URL"
echo "  AIOS_ROOT=$AIOS_ROOT"
echo ""

# 1. Ollama acessível
echo "1. Ollama endpoint"
if curl -sf "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  ok "Ollama responde em $OLLAMA_URL"
else
  fail "Ollama inacessível em $OLLAMA_URL"
fi

# 2. Modelos presentes
echo "2. Modelos"
MODELS_JSON=$(curl -sf "$OLLAMA_URL/api/tags" 2>/dev/null || echo '{"models":[]}')
for model in "${REQUIRED_MODELS[@]}"; do
  if echo "$MODELS_JSON" | python3 -c "import sys,json; d=json.load(sys.stdin); names=[m['name'] for m in d.get('models',[])]; sys.exit(0 if '$model' in names else 1)" 2>/dev/null; then
    ok "modelo '$model' presente"
  else
    fail "modelo '$model' em falta — instalar: curl -X POST $OLLAMA_URL/api/pull -d '{\"name\":\"$model\"}'"
  fi
done

# 3. Aider instalado
echo "3. Aider"
if command -v "$AIDER_BIN" >/dev/null 2>&1; then
  ver=$("$AIDER_BIN" --version 2>/dev/null | head -1)
  ok "aider instalado: $ver"
elif [ -f "$HOME/.local/bin/aider" ]; then
  ok "aider em ~/.local/bin/aider"
  export PATH="$HOME/.local/bin:$PATH"
else
  fail "aider não encontrado — instalar: pip3 install --user aider-chat"
fi

# 4. Estrutura de agentes
echo "4. Estrutura agents/"
for f in \
  "$AIOS_ROOT/agents/engineer/engineer_agent.py" \
  "$AIOS_ROOT/agents/reviewer/reviewer_agent.py" \
  "$AIOS_ROOT/agents/executor/executor_agent.py" \
  "$AIOS_ROOT/bin/agent_pipeline.py" \
  "$AIOS_ROOT/config/local_ai.json"; do
  if [ -f "$f" ]; then
    ok "$(basename $f)"
  else
    fail "em falta: $f"
  fi
done

# 5. Runtime dirs
echo "5. Runtime dirs"
for d in \
  "$AIOS_ROOT/runtime/agent_memory" \
  "$AIOS_ROOT/runtime/agent_logs"; do
  if [ -d "$d" ]; then
    ok "$d"
  else
    fail "dir em falta: $d (criar: mkdir -p $d)"
  fi
done

# 6. Teste rápido Ollama (sem Aider)
echo "6. Teste inferência"
RESP=$(curl -sf -X POST "$OLLAMA_URL/api/generate" \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5:14b","prompt":"responde apenas OK","stream":false}' \
  --max-time 30 2>/dev/null | python3 -c "import sys,json; print(json.load(sys.stdin).get('response','ERR')[:20])" 2>/dev/null || echo "TIMEOUT")
if [[ "$RESP" != "TIMEOUT" && "$RESP" != "ERR" && -n "$RESP" ]]; then
  ok "inferência OK: '$RESP'"
else
  fail "inferência falhou ou timeout"
fi

echo ""
echo "=== Resultado: $PASS OK, $FAIL FAIL ==="
[ "$FAIL" -eq 0 ] && echo "Sistema pronto." || echo "Corrigir itens FAIL antes de usar."
exit "$FAIL"
