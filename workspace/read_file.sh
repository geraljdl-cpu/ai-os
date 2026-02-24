#!/usr/bin/env bash
set -euo pipefail
path="$1"
curl -s -X POST http://127.0.0.1:8020/read -H "Content-Type: application/json" \
  -d "{\"path\":\"$path\"}"
