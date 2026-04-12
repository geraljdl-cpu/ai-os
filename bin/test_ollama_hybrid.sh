#!/usr/bin/env bash
# test_ollama_hybrid.sh — testa inferência em cada endpoint activo
set -euo pipefail

PROMPT='{"prompt":"def fib(n): return","stream":false,"options":{"num_predict":20}}'

test_inference() {
  local name="$1" url="$2" model="$3"
  local result tok dur tps
  printf "  Testing %-12s (%s)... " "$name" "$model"
  result=$(curl -s --connect-timeout 3 -X POST "$url/api/generate" \
    -H "Content-Type: application/json" \
    -d "{\"model\":\"$model\",$(echo "$PROMPT" | sed 's/^{//')}" 2>/dev/null)
  if [ -z "$result" ]; then
    echo "❌ DOWN"
    return
  fi
  tps=$(echo "$result" | python3 -c "
import json,sys
r=json.load(sys.stdin)
tok=r.get('eval_count',0)
dur=r.get('eval_duration',1)/1e9
print(f'{tok/dur:.1f} tok/s' if dur>0 else '?')
" 2>/dev/null || echo "parse error")
  echo "✅ $tps"
}

echo "=== Ollama Inference Test ==="
echo ""
echo "TIER 1 — asus_gpu"
test_inference "asus_gpu"  "http://localhost:11434" "qwen2.5-coder:7b"

echo ""
echo "TIER 2 — cluster_cpu"
test_inference "node1_cpu" "http://192.168.1.210:11434" "qwen2.5-coder:7b"
test_inference "node2_cpu" "http://192.168.1.211:11434" "qwen2.5-coder:7b"
echo ""
