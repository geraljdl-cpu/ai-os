#!/usr/bin/env bash
set -euo pipefail

curl -sS -X POST http://localhost:5679/autopilot \
  -H "Content-Type: application/json" \
  -d '{"goal":"Se existirem tarefas internas processa-as. Se não existirem responde apenas IDLE sem usar ferramentas."}' \
  >/dev/null
