#!/usr/bin/env bash
# check_ollama_hybrid.sh — verifica estado de todos os Ollama endpoints

check() {
  local name="$1" url="$2" tier="$3"
  local result models
  result=$(curl -s --connect-timeout 2 "$url/api/tags" 2>/dev/null || true)
  if [ -z "$result" ]; then
    printf "  %-12s [T%s] %-35s DOWN\n" "$name" "$tier" "$url"
    return
  fi
  models=$(echo "$result" | python3 -c "import json,sys; r=json.load(sys.stdin); print(','.join([m['name'] for m in r.get('models',[])[:2]]))" 2>/dev/null || echo "?")
  printf "  %-12s [T%s] %-35s UP   %s\n" "$name" "$tier" "$url" "$models"
}

echo "=== Ollama Hybrid Status ==="
echo "TIER 1 — asus_gpu"
check "nodegpu"   "http://192.168.1.202:11434" "1"

echo "TIER 2 — cluster_cpu"
check "node1_cpu" "http://192.168.1.210:11434" "2"
check "node2_cpu" "http://192.168.1.211:11434" "2"

echo ""
echo "Router:"
cd ~/ai-os && python3 bin/model_router.py status 2>/dev/null || echo "  router error"
