#!/usr/bin/env bash
set -e

case "$1" in
  create)
    curl -sS "$N8N_URL/api/v1/workflows" \
      -H "X-N8N-API-KEY: $N8N_API_KEY" \
      -H "Content-Type: application/json" \
      --data-binary "@$2"
    echo
    ;;
  update)
    curl -sS -X PUT "$N8N_URL/api/v1/workflows/$3" \
      -H "X-N8N-API-KEY: $N8N_API_KEY" \
      -H "Content-Type: application/json" \
      --data-binary "@$2"
    echo
    ;;
  activate)
    curl -sS -X POST "$N8N_URL/api/v1/workflows/$2/activate" \
      -H "X-N8N-API-KEY: $N8N_API_KEY"
    echo
    ;;
  deactivate)
    curl -sS -X POST "$N8N_URL/api/v1/workflows/$2/deactivate" \
      -H "X-N8N-API-KEY: $N8N_API_KEY"
    echo
    ;;
  list)
    curl -sS "$N8N_URL/api/v1/workflows" \
      -H "X-N8N-API-KEY: $N8N_API_KEY"
    echo
    ;;
  *)
    echo "Uso:"
    echo "  n8n_apply.sh create workflow.json"
    echo "  n8n_apply.sh update workflow.json ID"
    echo "  n8n_apply.sh activate ID"
    echo "  n8n_apply.sh deactivate ID"
    echo "  n8n_apply.sh list"
    ;;
esac
