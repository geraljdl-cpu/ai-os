# AI-OS (agent-core)

API + robot de execução de jobs (AI / shell / code) a correr em `http://localhost:8010`.

## Endpoints (porta 8010)

- `GET /health`
- `GET /jobs`
- `GET /jobs/{id}`
- `POST /jobs/{id}/update`
- `GET /events`
- `POST /events`
- `POST /think` (modo `local` → Ollama)
- `POST /enqueue_ai`
- `POST /enqueue_shell`
- `POST /enqueue_code`

## Stack (estado atual)

- `ollama` (Docker, **não** publicar `11434` para o host)
- `openwebui` em `http://localhost:8080`
- `postgres` + `redis`
- `agent-core` em `http://localhost:8010`
- `robot` (consome jobs e grava resultados)
- Outros serviços (n8n, Node-RED, Mosquitto, Qdrant, MinIO) — não mexer agora

## Testes rápidos (prova)

### 1) Think (Ollama via agent-core)
```bash
curl -s -X POST http://localhost:8010/think \
  -H "Content-Type: application/json" \
  -d '{"mode":"local","prompt":"responde só com a palavra: OK"}'

ok
