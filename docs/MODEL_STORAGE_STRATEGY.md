# MODEL_STORAGE_STRATEGY.md
# AI-OS — Model Storage Strategy (nodegpu)
Updated: 2026-04-10

---

## 1. Current State

Ollama runs on nodegpu (192.168.1.120, RTX 3090, 24 GB VRAM).
Models are stored in the Ollama data directory on the system disk.

Disk layout (nodegpu):
```
/dev/sda2   219 GB total   46 GB used   162 GB free   (single partition, /)
```

There is no `/fast` partition and no separate SSD for models. The entire disk
is one partition (`/dev/sda2`), so "active" model storage is effectively the
system SSD.

Ollama is not running in Docker on nodegpu — it runs as a native service.
Models are stored under the default Ollama data directory (`~/.ollama/models`
or the configured `OLLAMA_MODELS` path).

NAS (node-nas 192.168.1.118) is **currently offline**. The archive path
`/cluster/models_archive` is inaccessible until NAS is restored.

---

## 2. Storage Tiers

| Tier | Location | Status | Notes |
|---|---|---|---|
| Active (fast) | nodegpu `/` (SSD) | Implemented | Ollama native, full GPU speed |
| Fast partition `/fast` | Not created | Planned | No separate SSD partition exists yet |
| Archive (cold) | node-nas `/cluster/models_archive` | Offline | NAS at 192.168.1.118 unreachable |

There is no cold-storage tier in operation. Model archival is not possible
until node-nas is restored.

---

## 3. Model Inventory

All models confirmed present on nodegpu as of 2026-04-10 (via `GET /api/tags`):

| Model | Size | Use |
|---|---|---|
| qwen2.5-coder:14b | 9.0 GB | engineer_agent — code generation via Aider |
| qwen2.5:14b | 9.0 GB | reviewer_agent — diff review; general analysis |
| deepseek-r1:14b | 9.0 GB | reasoning/debug (installed; not yet wired into routing) |
| mistral:7b | 4.4 GB | fast/light tasks (installed; not yet wired) |
| nomic-embed-text:latest | 0.3 GB | embeddings (installed; RAG not yet implemented) |

Total on disk: ~31.7 GB across 5 models.

`config/local_ai.json` lists deepseek-r1:14b and mistral:7b as planned routes
(`reasoning` and `fast`), but neither is currently dispatched by any agent or
scheduler rule.

---

## 4. VRAM Budget

| Spec | Value |
|---|---|
| GPU | RTX 3090 |
| VRAM | 24 GB |
| Max loaded models | 1 at a time (Ollama default) |
| VRAM per 14b model | ~9 GB (Q4 quant) |
| VRAM per 7b model | ~4.4 GB |
| Headroom | ~15 GB free when one 14b loaded |

Ollama swaps models on demand. Loading a second 14b model while one is active
causes eviction of the first. This is acceptable given current single-model
workloads; do not attempt concurrent multi-model pipelines without testing.

**Constraint:** Never run active inference from NAS — NFS latency (even at
gigabit) makes token generation impractically slow for interactive use.

---

## 5. Scripts

| Script | Purpose |
|---|---|
| `bin/setup_models.sh [ollama_url]` | Pull required models (qwen2.5-coder:14b, qwen2.5:14b). Pass `INSTALL_OPTIONAL=1` to also pull deepseek-r1:14b and mistral:7b |
| `bin/model_sync.sh [model_name]` | Copy model blobs from NAS archive → `/fast/models`. Currently exits early with a warning because NAS is offline |
| `bin/validate_local_agent.sh` | Verifies Ollama reachable, required models present, Aider installed |

---

## 6. Future: /fast Partition

When a dedicated SSD or partition becomes available on nodegpu:

1. Create partition, format ext4, mount at `/fast`
2. Create `/fast/models` directory
3. Stop Ollama service
4. Move Ollama models dir: `mv ~/.ollama/models /fast/models`
5. Set `OLLAMA_MODELS=/fast/models` in Ollama service environment
6. Restart Ollama
7. Update `bin/model_sync.sh` — `FAST_MODELS` env var already points to `/fast/models`

Until then, `model_sync.sh` NAS→fast copy path is a no-op (NAS offline + no
`/fast` mount).

---

## 7. Operational Notes

- Do not run `docker volume rm` on any Ollama volume — this is in the executor
  ALWAYS_DENY list and would destroy all downloaded models.
- Model pulls are done via `POST /api/pull` (setup_models.sh) or `ollama pull`
  on nodegpu. Each 14b model takes ~9 GB download.
- After pulling a new model, no service restart required — Ollama hot-loads.
- Disk usage check: `ssh jdl@192.168.1.120 "df -h /"`
- Model list check: `curl -s http://192.168.1.120:11434/api/tags | python3 -c "import sys,json; [print(m['name'], round(m['size']/1e9,1),'GB') for m in json.load(sys.stdin)['models']]"`
