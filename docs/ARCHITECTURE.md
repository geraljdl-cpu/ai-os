# AI-OS — Arquitectura

> Gerado: 2026-03-03 | Stack: Python 3 · Node.js · PostgreSQL · Docker

---

## Componentes e Portas

| Componente | Porta | Tecnologia | Descrição |
|---|---|---|---|
| **UI / API** | 3000 | Express (Node.js, PM2) | Dashboard SPA + API REST com JWT |
| **Caddy HTTPS** | 8443 | Caddy 2.11 | Reverse proxy TLS self-signed → Express |
| **Agent-Router** | 5679 | Docker (n8n/custom) | Recebe goals, devolve steps JSON |
| **PostgreSQL** | 5432 | Docker (`postgres`) | Jobs, approvals, users, audit_log |
| **Modbus TCP** | 5020 | pymodbus 3.x | Simulador de sensores industriais |
| **Art-Net UDP** | 6454 | Python stdlib | Simulador DMX/Art-Net |
| **Caddy Admin** | 2019 | Caddy | API interna de reload |

---

## Flow do Autopilot

```
┌─────────────────────────────────────────────────────┐
│  aios-worker.service  (systemd --user)               │
│                                                       │
│  autopilot_loop.sh                                    │
│    └─ loop 5s ────────────────────────────────────┐  │
│                                                    │  │
│  autopilot_worker.sh (batch, MAX_TASKS=5, 45s)    │  │
│    1. backlog_pg.peek_next_task_json()             │  │
│    2. POST /agent → agent-router:5679              │  │
│    3. tools_engine.py executa steps               │  │
│    4. Success Gate (ok:false/blocked → failed)    │  │
│    5. backlog_pg.update_task(done|failed)         │  │
│    └────────────────────────────────────────────┘ │  │
└───────────────────────────────────────────────────┘  │
                                                        │
                         ←  sleep 5s  ←────────────────┘
```

---

## Módulos Principais

### `bin/backlog_pg.py`
- Source of truth do backlog: Postgres com fallback JSON
- API: `add_task`, `list_tasks`, `get_next_task`, `update_task`, `autofill_if_empty`
- `autofill_if_empty(n=4)` — preenche com 4 tarefas housekeeping seguras quando vazio
- CLI: `python3 bin/backlog_pg.py status|add|list|next`

### `bin/tools_engine.py`
- Dispatcher de tools com allowlist e sandbox
- Tools base: `bash_safe`, `write_file`, `read_file`, `git_commit`, `git_status`
- Tools carregadas por importlib: `tools_finance`, `tools_factory`, `tools_dmx`
- Approval gate para `toc_invoice_create`, `git_commit`, `write_file` em modo `live`

### `bin/approval_pg.py`
- Workflow de aprovação humana antes de tools destrutivas
- Persiste em Postgres + sync JSON (`runtime/pending_approvals.json`)
- CLI: `python3 bin/approval_pg.py list|approve <id>|reject <id>`

### `bin/secrets.py`
- AES-256-GCM, chave mestra em `~/.aios_master_key` (600)
- Store cifrado em `runtime/secrets.json`
- API: `get_secret(name)`, `set_secret(name, value)`

### `bin/tools_finance.py`
- Integração TOConline (faturas, clientes)
- OAuth2: token em `~/.toc_token.json`, renovação automática
- Tools: `toc_invoices`, `toc_customers`, `toc_invoice_create` (requer aprovação)

### `bin/tools_factory.py` + `bin/modbus_simulator.py`
- Leitura de registos Modbus TCP (addr 0-3: Temp, Pressão, RPM, Estado)
- Simulador em `systemd/aios-modbus.service`

### `bin/tools_dmx.py` + `bin/artnet_simulator.py`
- Controlo DMX via Art-Net UDP (8 cenas pré-definidas)
- Simulador em `systemd/aios-artnet.service`

---

## Base de Dados (PostgreSQL `aios_db`)

| Tabela | Descrição |
|---|---|
| `jobs` | Tarefas do backlog (status: pending/running/done/failed/skipped/archived) |
| `steps` | Steps individuais de cada job |
| `approvals` | Aprovações pendentes/aprovadas/rejeitadas |
| `audit_log` | Imutável (sem UPDATE/DELETE via ORM) |
| `users` | Utilizadores com roles (admin/operator/viewer) |
| `roles` | Definição de papéis RBAC |

Credenciais: `~/.env.db` → `DATABASE_URL`, `JWT_SECRET`

---

## Timers e Serviços Systemd

| Unit | Tipo | Descrição |
|---|---|---|
| `aios-worker.service` | user service | Autopilot loop — `Restart=always` |
| `aios-backup.timer` | timer diário 3h | pg_dump → `backups/` com restore test |
| `aios-caddy.service` | user service | Caddy HTTPS proxy |
| `aios-modbus.service` | system service | Simulador Modbus TCP 5020 |
| `aios-artnet.service` | system service | Simulador Art-Net UDP 6454 |

---

## Observabilidade

```bash
# Estado do worker
systemctl --user status aios-worker

# Logs em tempo real (filtra ruído idle)
journalctl --user -u aios-worker -f

# Snapshot do sistema
python3 bin/system_snapshot.py

# Relatório de falhas
python3 bin/fail_report.py

# Métricas do worker
python3 bin/worker_metrics.py
```

---

## Segurança

- JWT HS256 em todas as rotas `/api/*` (excepto `/api/auth/login`)
- Rate limiting: 100 req/min por IP (in-memory no Express)
- Allowlist de comandos bash: `echo, cat, ls, mkdir, cp, mv, python3, find, head, tail, grep, wc`
- Sandbox de paths: `write_file` só escreve dentro de `AIOS_ROOT`
- Secrets AES-256-GCM (nunca em texto claro em ficheiros versionados)
