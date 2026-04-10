# LOCAL_AGENT_BOOTSTRAP.md
# AI-OS — Local Agent Stack & Project Reference
Updated: 2026-04-10

---

## 1. Repository

Main repo on WSL2: `~/ai-os` (`/home/jdl/ai-os`)
Cluster read path: `/cluster/d1/ai-os` (NFS from 192.168.1.126)

Key directories:

| Dir | Contents |
|---|---|
| `agents/` | engineer/, reviewer/, executor/ — local LLM agent modules |
| `bin/` | All scripts and daemons (agent_pipeline.py, pipeline_scheduler.py, …) |
| `config/` | local_ai.json, cluster_workers.json |
| `docs/` | Architecture docs, runbooks, this file |
| `sql/` | 57+ numbered migration files |
| `ui/` | server.js (Express :3000) + HTML dashboards |
| `workers/` | systemd .service/.timer units + cluster_worker.py + install scripts |
| `systemd/` | Additional WSL2-local systemd units |
| `runtime/` | Logs, agent_memory, worker output (gitignored, created at runtime) |
| `pylib/` | Pure-Python deps on NFS (pg8000, anthropic, …) for cluster nodes |

---

## 2. Architecture

```
                        ┌─────────────────────────────────┐
                        │  WSL2 / ASUS laptop (control)   │
                        │  PostgreSQL :5432 (Docker)       │
                        │  Express UI :3000                │
                        │  pipeline_scheduler (60s timer)  │
                        └────────────────┬────────────────┘
                                         │ worker_jobs queue
              ┌──────────────────────────┼──────────────────────┐
              │                          │                      │
        node2 ai_analysis         node4 radar/automation  node3 preprocess/general
        node5 general             node6 general           node7 watchdog/light/echo
              │                                                  │
              └──────────────────── NFS /cluster/d1/ai-os ──────┘
                                         │
                                    nodegpu RTX3090
                                    Ollama :11434
                                    llm_gpu / ai_analysis
```

All cluster nodes mount NFS. The codebase is shared; each node runs
`cluster_worker.py` via `aios-cluster-worker.service` (systemd user service).

---

## 3. Node Roles

| Node | IP | Roles | Notes |
|---|---|---|---|
| control (WSL2) | 172.22.x.x | scheduler, DB, UI | pipeline_scheduler, autonomia_orchestrator |
| nodegpu | 192.168.1.120 | llm_gpu, ai_analysis | RTX3090 24GB VRAM, Ollama |
| nodecpu | 192.168.1.121 | preprocess, general | was node3 |
| node2 | 192.168.1.112 | ai_analysis | Strategist — Claude API |
| node4 | 192.168.1.122 | radar, automation, ai_analysis | Operations |
| node5 | 192.168.1.123 | general | Finance — phase-out pending |
| node6 | 192.168.1.124 | general | Radar — phase-out pending |
| node7 | 192.168.1.125 | watchdog, light, echo, fallback | Watchdog |
| node-nas | 192.168.1.118 | NAS storage | Currently offline |

Node roles and DB config are in `config/cluster_workers.json`.

---

## 4. Cluster Assumptions

- NFS codebase path: `/cluster/d1/ai-os/` (not `/cluster/ai-os/`)
- DB: `postgresql+pg8000://aios_user:jdl@192.168.1.172:5432/aios`
  (192.168.1.172 = Windows host portproxy → WSL2 :5432)
- Python deps on NFS: `PYTHONPATH=/cluster/d1/ai-os/pylib` (pg8000, anthropic, etc.)
- Never add `bin/` to PYTHONPATH in subprocess calls — `bin/secrets.py` shadows stdlib `secrets`
- Node identity: env var `AIOS_NODE_NAME` (e.g. `node2`); cluster_worker.py reads this
- Cluster worker logs: `/cluster/d1/ai-os/runtime/workers/<node_name>/worker.log`
- `loginctl enable-linger` required on each node so user services survive logout

---

## 5. UI / Dashboards

Served by `ui/server.js` (Express :3000), managed by `aios-ui.service`.
Always restart with `sudo systemctl restart aios-ui.service`, never PM2.

| Dashboard | URL | Purpose |
|---|---|---|
| joao.html | /joao | Main operator dashboard — ideias, tarefas, timesheets, despesas, seguros |
| noc.html | /noc | Public NOC — 6 blocks, 16:9, dark, auto-refresh 5s |
| control.html | /control | Cluster control plane / worker status |
| factory.html | /factory | Serviços/turnos operacionais |
| finance.html | /finance | Obrigações, faturas, pagamentos |
| ops.html | /ops | Incidents, operações |
| tenders.html | /tenders | Radar concursos |
| validar.html | /validar | Client timesheet validation (token-based) |
| conta.html | /conta | Client accounting portal |
| worker.html | /worker | Worker timesheet entry |

---

## 6. SQL Migrations

57+ files in `sql/`, numbered by domain:

| Range | Domain |
|---|---|
| 000–001 | Base infra, schema patches |
| 210–213 | Radar (raw items, normalized, scores, runs) |
| 220–231 | Invoices, timesheets, finance obligations |
| 310–395 | Agent suggestions, bank recon, incidents, ideas, cluster metrics, worker jobs retry, documents, vehicles, entities, seed data |
| 400–495 | Council, document types, WhatsApp, commercial, insurance, workspace |
| 500–570 | Validation enhanced, client roles, RH, prompt inbox, autonomia, client invoices, service planning, marketplace |

Run in order. Files are idempotent (use `IF NOT EXISTS` / `DO $$ … $$`).

---

## 7. Automation / Timers

| Unit | Interval | Function |
|---|---|---|
| `aios-pipeline-scheduler.timer` | 60s | Enqueues jobs: ideas, incidents, radar, finance, briefing, closing, radar_gpu |
| `aios-autonomia-orchestrator.timer` | 60s | Autonomia goals loop |
| `aios-cluster-telemetry.timer` | 30s | Node metrics → cluster_node_metrics |
| `aios-insurance-alerts.timer` | daily 07:30 | Insurance expiry alerts |
| `aios-finance-alerts.timer` | daily | Finance obligation alerts |
| `aios-doc-alerts.timer` | daily | Document expiry alerts |
| `aios-radar-base.timer` | daily | base.gov radar pipeline |
| `aios-radar-dr.timer` | daily | Diário da República radar pipeline |
| `aios-backup.timer` | daily | Postgres backup |
| `aios-smoke.timer` | periodic | Smoke tests |
| `aios-cluster-worker.service` | always-on | Worker daemon on each node (Restart=always) |

`pipeline_scheduler.py` pipelines and their cooldowns:
- ideas: 1 min — open idea threads with joao messages → ai_analysis
- incidents: 10 min → automation
- radar TED: 30 min → radar; base.gov + DR: daily → automation
- finance: 1 h → automation; obligation alert: on-demand
- stale cases: 2 h → general
- briefing: 08:25–09:05 UTC → general
- closing: 17:25–18:05 UTC → general
- radar_gpu: top tenders score≥40, max 3/tick → llm_gpu on nodegpu

---

## 8. Local Agent Stack

### Components

| File | Role |
|---|---|
| `agents/engineer/engineer_agent.py` | Runs Aider + qwen2.5-coder:14b against repo |
| `agents/reviewer/reviewer_agent.py` | Calls qwen2.5:14b via Ollama API to review diff |
| `agents/executor/executor_agent.py` | Applies approved diff via `git apply` |
| `bin/agent_pipeline.py` | Orchestrates engineer → reviewer → executor |
| `config/local_ai.json` | Model routing config (nodegpu 192.168.1.120:11434) |
| `bin/ai-code` | Interactive Aider wrapper for nodegpu sessions |
| `bin/validate_local_agent.sh` | Validates Ollama, models, Aider, file structure |
| `bin/setup_models.sh` | Pulls required models into Ollama |
| `bin/model_sync.sh` | Syncs models from NAS archive to active storage |

### Reviewer safety rules

Static DENY_PATTERNS checked before LLM call (in `reviewer_agent.py`):
`rm -rf`, `DROP TABLE`, `DROP DATABASE`, `TRUNCATE`, `os.system(`, `eval(`, `exec(`, etc.

Executor ALWAYS_DENY (in `executor_agent.py`):
`rm -rf`, `docker system prune`, `docker volume rm`, `shutdown`, `mkfs`, `dd if=`, etc.

### Usage

**Interactive (on nodegpu):**
```bash
ssh jdl@192.168.1.120
ai-code ~/ai-os                          # qwen2.5-coder:14b default
ai-code --model ollama/qwen2.5:14b       # general model
```
`ai-code` sets `OLLAMA_API_BASE=http://localhost:11434` and launches Aider with `--no-auto-commits`.

**Pipeline (from WSL2):**
```bash
# Propose, review, apply (human approval prompt):
python3 bin/agent_pipeline.py "adicionar type hints a bin/foo.py" --files bin/foo.py

# Fully automated (reviewer APPROVED → apply without prompt):
python3 bin/agent_pipeline.py --auto "fix: corrigir import em bin/bar.py"

# Dry-run (engineer + reviewer only, no apply):
python3 bin/agent_pipeline.py --dry-run "descrever alteração"

# Override model:
python3 bin/agent_pipeline.py --model qwen2.5:14b "arquitectura nova feature"
```

Pipeline outcomes: `done` | `dry_run` | `rejected` | `needs_revision` | `error`
History: `runtime/agent_memory/pipeline_history.jsonl`
Logs per stage: `runtime/agent_logs/engineer_*.json`, `reviewer_*.json`, `executor_*.json`

**Validation:**
```bash
bash bin/validate_local_agent.sh          # checks Ollama, models, Aider, runtime dirs
bash bin/setup_models.sh                  # install qwen2.5-coder:14b + qwen2.5:14b
INSTALL_OPTIONAL=1 bash bin/setup_models.sh  # also install deepseek-r1:14b + mistral:7b
```

---

## 9. Current Risks

| Risk | Status |
|---|---|
| node-nas (192.168.1.118) offline | NAS unreachable — model_sync.sh exits early; no archive |
| node5/node6 phase-out pending | Both run only `general` role; migrate workload to nodecpu first |
| NEEDS_REVISION not retried | Pipeline exits on NEEDS_REVISION; manual re-run required |
| No git commit step in executor | `git apply` succeeds but no commit created automatically |
| agent_pipeline not connected to worker_jobs | Pipeline is CLI-only; not yet enqueued from DB scheduler |
| nodegpu is single point of failure for LLM | No fallback if nodegpu is down; reviewer falls back to REJECTED |

---

## 10. Next 10 Highest-Value Improvements

1. **Phase out node5/node6** — migrate `general` jobs to nodecpu; update `config/cluster_workers.json`
2. **Restore node-nas** — investigate 192.168.1.118; re-enable model_sync.sh archive path
3. **Add git commit step in executor** — after successful `git apply`, run `git commit -m "aios: <goal>"`
4. **Add engineer retry loop** — when reviewer returns NEEDS_REVISION, re-run engineer with reviewer feedback (max 2 retries)
5. **Connect agent_pipeline to worker_jobs** — add `llm_engineer` kind to pipeline_scheduler; cluster_worker dispatches to agent_pipeline
6. **Create noc.html widget for agent_pipeline activity** — show last 5 pipeline runs from `pipeline_history.jsonl`
7. **Add nomic-embed-text RAG** — already installed on nodegpu (0.3 GB); wire up to idea search / document similarity
8. **Add `gpu_inference` / `llm_gpu` worker handler** — extend cluster_worker.py on nodegpu to handle `llm_gpu` kind (currently scheduled by pipeline_scheduler but no handler)
9. **Deepseek-r1:14b already installed on nodegpu** — route reviewer to deepseek for architecture/debug tasks (update `config/local_ai.json` routing)
10. **Add mistral:7b classify/light handler** — already installed; use for fast classification jobs in watchdog/light roles
