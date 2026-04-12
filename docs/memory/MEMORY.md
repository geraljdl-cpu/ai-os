# Claude Code Memory

## AI-OS Project
Root: `~/ai-os` | Stack: Postgres (docker), agent-router:5679, agent-core, redis, ollama
**Fase actual: escala funcional** — cluster distribuído activo, /joao fecha ciclo ideia→análise→projeto

## Ollama Hybrid Architecture — activo 2026-03-21
- ASUS Docker Ollama: GPU RTX 4050, 28 tok/s, localhost:11434 (nvidia-container-toolkit instalado)
- TIER 1 asus_gpu: localhost:11434 → qwen2.5-coder:7b (GPU)
- TIER 2 cluster_cpu: node1(tunnel:11435), node2(192.168.1.211:11434), node4(192.168.1.122:11434)
- NFS models: /cluster/d1/ollama/models/ — partilhado entre nodes
- NFS binary: /cluster/d1/ollama/bin/ollama — instalado via user systemd (aios-ollama.service)
- model_router.py v2: PROVIDERS registry, healthcheck, routing, CLI `health`/`status`/`set_override`
- prompt_inbox_worker.py: target='local'/'asus_gpu'/'cluster_cpu' → Ollama, fallback Claude
- Scripts: bin/check_ollama_hybrid.sh, bin/test_ollama_hybrid.sh
- Docs: docs/OLLAMA_HYBRID.md, docs/OLLAMA_AIDER.md
- Aider: OLLAMA_API_BASE=http://localhost:11434 aider --model ollama/qwen2.5-coder:7b --no-auto-commits
- node3(8GB) e node5-7(3.9GB) não têm Ollama (RAM insuficiente para 7b)

## CRITICAL: Session Start Protocol
O Claude Code linter reverte ficheiros tracked do git em novas sessões. Ao iniciar sessão SEMPRE restaurar:
```bash
git checkout 2b85c3f -- bin/telegram_bot.py bin/service_billing.py bin/noc_query.py ui/server.js ui/joao.html ui/noc.html ui/validar.html
sudo systemctl restart aios-telegram.service aios-ui.service
```
Commit `2b85c3f` = Sprint RH + Prompt Inbox (branch aios/20260316_084144_c0deaab9)

## Pitfall: joao.html redirect loop (rate limit)
- TOKEN expirado no localStorage → api() devolve 401 → redirect /login → login vê TOKEN → redirect /joao → loop
- Fix: `localStorage.removeItem('aios_jwt')` ANTES de `location.href='/login'` no handler de 401
- Rate limit server.js: subido para 500 req/min (loadOverview faz ~10 requests em paralelo)

## Validação Enhanced + Client Roles — activo 2026-03-20
- SQL: `sql/500_validation_enhanced.sql` — colunas adjusted_* + client_extras + approved_by em event_timesheets + tabela timesheet_expenses
- SQL: `sql/510_client_roles.sql` — roles client_manager + client_accounting + tabela clients + users.client_id
- service_billing.py: get_with_expenses, add_expense, review_expense, mark_reimbursed, validate (agora suporta adjusted_days/extras/expense_decisions)
  - New statuses: approved_client / adjusted_client / rejected_client / reimbursed_mbway
  - generate_invoice_draft: suporta extras (linhas múltiplas em metadata.lines)
- server.js: GET /api/service/validate/:token chama get_with_expenses; POST aceita adjusted_days/extras/expense_decisions
  - Novos: POST expenses, GET/POST photo, POST reimburse, GET timesheets/:id/expenses
  - Novos: /api/client/* (overview/timesheets/expenses/invoices/monthly) com requireClientAccess
  - /conta.html servida em /conta
- validar.html: 5 secções (resumo, ajuste dias, extras, despesas MB WAY, preview)
- joao.html: modal 💳 Despesas por turno (lista despesas + form lançar nova + upload foto)
- conta.html: dashboard cliente_accounting (KPIs, tabs: serviços/despesas/faturas/mensal)
- auth.py: create_token aceita client_id; login devolve client_id; create_user aceita --client-id
- Criar utilizador cliente: `python3 bin/auth.py create <user> <pass> client_accounting --client-id <N>`
- Criar cliente: POST /api/clients (admin) ou INSERT directo em public.clients

## Módulo Seguros — activo 2026-03-17
- SQL: `sql/480_insurance.sql` — 4 tabelas (policies, documents, alerts, vehicles_link)
- Backend: `bin/insurance_engine.py` — CRUD + parse_document() regex + generate_alerts() + CLI
- Job diário: `bin/insurance_alerts.py` — Telegram + email às 07:30 (aios-insurance-alerts.timer)
- noc_query: `insurance_policies/policy_get/docs/alerts/stats`
- server.js: 10+ rotas `/api/insurance/*`
- joao.html: tab 🛡️ Seguros com KPIs, alertas, apólices (dias restantes coloridos), modal detalhe + add
- ALERT_THRESHOLDS: [365,180,90,60,30,15,7,1] (estendido)
- 7 apólices: 5 Fidelidade particular + 2 empresa (756999316 frota, AT64854896 AT)

## Fidelidade Scraper — activo 2026-03-18
- `bin/fidelidade_scraper.py` — suporta portal pessoal E empresa
- Portal pessoal: `python3 bin/fidelidade_scraper.py` (usa FIDELIDADE_NIF/PASS de /etc/aios.env)
- Portal empresa: `python3 bin/fidelidade_scraper.py --empresa`
- Credenciais empresa: NIF pessoal 209244216, PASS @Jdsilva1 (mesmo NIF nos 2 campos do form)
- Empresa JOAO DIOGO LOPES UNIP LDA — 2 apólices: 756999316 (frota 06-LQ-67+79-JS-11) + AT64854896 (3 trab.)
- Pitfall: NIF field type='tel' com IMask usa thousands sep \xa0 — regex deve usar `[0-9 \xa0.,]+`
- Pitfall: SPA empresa carrega dados async — sempre fazer goto(PoliciesList) antes de clicar cada apólice

## NOC Dashboard
- Rota: http://localhost:3000/noc — 6 blocos, 16:9, dark, sem scroll, 5s refresh
- Público (zero auth) — usa /api/control/overview
- Blocos: Cluster (nodes+telemetria), Factory, Finance, Radar jobs, Incidents, Ideias
- control/overview agora inclui: cluster_jobs (últimos 15) + cluster_metrics (latest per node)

## Cluster Telemetry — activo 2026-03-15
- Tabela: `public.cluster_node_metrics` (cpu, ram, load, disk, worker_state, jobs_24h, failures_24h)
- Script: `bin/cluster_telemetry.py` — runs on each node via systemd timer 30s
- Service files: `workers/aios-cluster-telemetry.{service,timer}` — installed on node2-node7
- noc_query: `cluster_metrics [limit]` — latest row per node
- Todos os 6 nodes a reportar. Prometheus-free — leve e funcional.

## Pipelines Cluster — activos 2026-03-15
- Pipeline 1: `ai_analysis` + `{"thread_id": N}` → idea_router.py → idea_reviews (Claude API)
  - `pipeline_idea_analyze <thread_id>` em noc_query.py
  - idea_create auto-enqueue quando tem mensagem
- Pipeline 2: `radar` + `{"script":"radar_score","args":["--source","ted"]}` → radar_scores
  - `pipeline_radar_score [source]` em noc_query.py
- Pipeline 3: `automation` + `{"cmd":"python3 /cluster/d1/ai-os/bin/incidents_tick.py"}` → events
  - `pipeline_incidents` em noc_query.py

## Cluster Distribuído (node2-node7) — estado: activo 2026-03-15

### Topologia
- NFS server: 192.168.1.126 → montado em /cluster em todos os nodes
- Codebase cluster: `/cluster/d1/ai-os/` (NOT /cluster/ai-os/)
- Windows host LAN: `192.168.1.101` (portproxy 5432→WSL2, 8010→WSL2)
- WSL2 IP: `172.22.158.152`

### Nodes e roles
- node1: 192.168.1.210 (control plane)
- node2: 192.168.1.211 → `ai_analysis`
- node3: 192.168.1.212 → `preprocess,general`
- node4: 192.168.1.122 → `radar,automation,ai_analysis`
- node5: 192.168.1.123 → `general`
- node6: 192.168.1.124 → `general`
- node7: 192.168.1.125 → `watchdog,light,echo,fallback`

### Ficheiros chave (cluster)
- `bin/cluster_worker.py` — runner único (lê config, lease SQL, dispatch, heartbeat)
- `config/cluster_workers.json` — roles por node + DB config
- `workers/aios-cluster-worker.service` — systemd user service (Restart=always)
- `workers/install_cluster.sh` — deploy em cada node (sem root, via NFS)
- `pylib/` em NFS — pg8000+sqlalchemy pure-Python (sem deps binárias)

### Operação cluster
```bash
# Enfileirar job
python3 ~/ai-os/bin/noc_query.py worker_jobs_enqueue "<role>" '<json_payload>' [node|-]
# Ver jobs
python3 ~/ai-os/bin/noc_query.py worker_jobs [limit]
# Logs por node
ssh nodeX "tail -f /cluster/d1/ai-os/runtime/workers/nodeX/worker.log"
# Restart worker
ssh nodeX "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user restart aios-cluster-worker.service"
# Re-deploy após mudanças no cluster_worker.py
scp ~/ai-os/workers/cluster_worker.py jdl@node2:/cluster/d1/ai-os/bin/cluster_worker.py
# e depois restart em todos os nodes
```

### Pitfalls cluster
- Nodes não têm pip nem ensurepip (Ubuntu 24.04 PEP 668) → usar pylib NFS com --target
- psycopg2-binary NÃO é portável entre máquinas (C extension) → usar pg8000 (pure Python)
- NFS path correcto é `/cluster/d1/ai-os/` (NÃO `/cluster/ai-os/`)
- DB password correta: `aios_user:jdl` (não aios2026)
- `bin/secrets.py` shadows stdlib `secrets` in subprocesses too
  Fix: NEVER add bin/ to PYTHONPATH in subprocess cmds (use inherited pylib PYTHONPATH only)
  Affected: scramp→pg8000→sqlalchemy chain fails if PYTHONPATH includes bin/
- anthropic SDK needs pylib on NFS: `PYTHONPATH=/tmp/mypip python3 -m pip install --target=/cluster/d1/ai-os/pylib anthropic`
  Must run on a cluster node (Python 3.12) NOT on WSL2 (Python 3.10) — compiled extensions (pydantic-core, jiter) are version-specific
- ANTHROPIC_API_KEY stored in `/etc/aios.env` — real key; bash env shows masked `sk-ant-...`
  For NFS .secrets: `KEY=$(grep ANTHROPIC /etc/aios.env | cut -d= -f2-) && ssh nodeX "printf 'ANTHROPIC_API_KEY=%s\n' '$KEY' > /cluster/d1/ai-os/config/.secrets"`
- config/.secrets loaded at cluster_worker.py startup → ANTHROPIC_API_KEY in subprocess env
  After changing .secrets, must restart ALL worker nodes that use ai_analysis
- systemd --user requer `loginctl enable-linger` para sobreviver a logout

### Key files
- `bin/autopilot_worker.sh` — main worker (batch loop, DIRECT_EXEC routing, pacing)
- `bin/backlog_pg.py` — task queue API (add_task, peek, update)
- `bin/tools_engine.py` — bash_safe executor (ALLOWED_CMDS, DENIED)
- `bin/aios_health.sh` — unified health check command
- `bin/aios_watchdog.sh` — auto-restart if stuck (pending>0, done_10m==0)
- `bin/status_server.py` — FastAPI dashboard (port 8000, uvicorn)

### Systemd units (prod-ready state)
- `aios-autopilot.timer` — every 30s, MAX_TASKS=5, MAX_SECS=45
- `aios-watchdog.timer` — every 60s, runs `bin/aios_watchdog_run.sh` (retry loop, max 20s)
- `aios-ui.service` — Express UI on :3000, `Restart=always`
- Hardening drop-in: `aios-autopilot.service.d/hardening.conf` (25/10s timeouts, AIOS_SLEEP_BETWEEN_TASKS=0.2)

### Prod endpoints
- UI: http://localhost:3000
- agent-router: http://localhost:5679/health → `{"ok":true}`
- agent-core: http://localhost:8010/docs

### UI — gestor: systemd ÚNICO (não PM2)
Existe `aios-ui.service` systemd E PM2 configurado para o mesmo server.js.
PM2 acumula milhares de restarts pois não consegue a porta 3000 (systemd já a tem).
**Usar SEMPRE: `sudo systemctl restart aios-ui.service`** para recarregar código novo.
NÃO usar pm2 para a UI — causa orphan storm + código antigo a responder.

### Pitfall: node órfão na porta 3000
Verificar: `systemctl status aios-ui.service | grep "Main PID"` deve coincidir com `ss -tlnp | grep 3000`
Fix se não coincidir: `sudo pkill -9 -f "node.*server.js"` + `sudo systemctl restart aios-ui.service`

### Pitfall: systemd Restart= inválido
`Restart=unless-stopped` é Docker, não systemd → serviço fica "dead".
Fix: usar `Restart=always` ou `Restart=on-failure`.

### Pitfall: aspas simples no ExecStart
`ExecStart=/usr/bin/env bash -lc '...'` → systemd recusa (unbalanced quoting).
Fix: extrair script para ficheiro `.sh` e referenciar diretamente no ExecStart.

### Teste de recuperação (amanhã)
```bash
docker stop agent-router
sleep 2
sudo systemctl start aios-watchdog.service
sleep 5
curl -fsS http://127.0.0.1:5679/health && echo ROUTER_RECOVERED_OK
```

### Task routing (autopilot_worker.sh)
- `_is_direct_exec()`: OPS_TASK || goal starts with curl/docker/git/python3 → bypass LLM
- DEV_TASK → SAFE MODE prompt → LLM → tools_engine
- TASK_TYPE extracted from task JSON; backlog_pg infers from goal prefix

### backlog_pg task_type inference
- `curl /docker /git /systemctl /apt ` prefix → OPS_TASK
- explicit valid type → respected
- otherwise → DEV_TASK

### Pitfalls learned
- `bin/secrets.py` shadows stdlib `secrets` → never use WorkingDirectory=.../bin for Python services
  Fix: `WorkingDirectory=/home/jdl/ai-os` + `python3 -m uvicorn bin.status_server:app`
- LLM generates invalid Python one-liners (try/except single-line SyntaxError) when forced to rewrite curl
  Fix: DIRECT_EXEC for curl goals, `curl` in ALLOWED_CMDS

### Alertas Telegram
- Token + chat_id guardados em `~/.env.db` (AIOS_TG_TOKEN, AIOS_TG_CHAT)
- Script: `bin/aios_alert.sh "mensagem"` — sai silenciosamente se vars não definidas
- Watchdog lê `.env.db` e envia alerta em cada recovery (router/core/postgres)

### Systemd units (prod-ready state)
- `aios-pgbackup.timer` — diário às 00:00, retém 14 backups em `runtime/backups/`

### Operational commands
```bash
~/ai-os/bin/aios_health.sh               # health check
http://localhost:8000/                    # dashboard UI
http://localhost:8000/status              # dashboard JSON
journalctl -u aios-watchdog.service -n 50 # watchdog history
sudo fuser -k ~/ai-os/runtime/autopilot.lock  # unblock lock (rare)
```

### DB (public.tasks)
- status: pending/done/failed/skipped/cancelled
- task_type: OPS_TASK / DEV_TASK / RESEARCH_TASK
- Enqueue: `python3 -c "import sys; sys.path.insert(0,'$HOME/ai-os'); from bin import backlog_pg; ..."`

## Session 2026-03-15 — estabilização pós-WSL reset + primeiro caso real
- Docker stack subiu via aios-docker-stack.service (bin/docker_stack_up.sh --pull never)
- DB migrations reaplicadas (32 tabelas); 3 serviços desativados (alerts/telemetry/status)
- Primeiro batch real: entity_id=4, case_id=4 (1200kg Salero Producao Lda, estado=chegou)
- **Pitfall: `public.events` estava com schema antigo** — faltavam cols level/kind/entity_id/message/data
  Fix: ALTER TABLE ADD COLUMN IF NOT EXISTS + ALTER COLUMN type SET DEFAULT 'event'
- **Pitfall: `public.twin_approvals` sem coluna `summary`** — twin_batch_faturar falha
  Fix: ALTER TABLE twin_approvals ADD COLUMN IF NOT EXISTS summary TEXT
- **Pitfall: control/overview retornava [] para cases/workers/tasks/tenders**
  noc_query commands retornam array direto, mas server.js esperava `{key:[]}` wrapper
  Fix: `const arr = v => Array.isArray(v) ? v : []` + usar `cases.cases || arr(cases)`
- **Pitfall: backlog_recent quebrado** — fazia SELECT title de public.jobs que não tem essa col
  Fix: usar `goal AS title` e `meta->>'task_type'` etc via meta JSONB

## Sprint P — Document Vault + Knowledge + Council (2026-03-15)
- Tabelas novas: `documents`, `document_requirements`, `document_requests`, `document_actions`, `vehicles`, `companies`, `persons`, `council_reviews`
- `bin/daily_briefing.py` — briefing 08:30 (finance/docs/radar/incidents/cases), aios-briefing-0830.timer
- `bin/knowledge.py` — Qdrant knowledge layer (collection='knowledge', 768-dim nomic-embed-text)
  Commands: add/search/list/delete | API: /api/knowledge (GET/POST/DELETE)
- `bin/council.py` — generic AI Council (4 agents: strategist/engineering/ops/finance + synthesis)
  Stores in `council_reviews`. API: POST /api/council/analyze | Telegram: /council <topic>
- noc.html: Block 9 CLUSTER RESOURCES (full-width table CPU/RAM/jobs/failures/role/estado)
- noc.html: Block 8 DOCUMENTOS (expired/expiring/pending counts + critical docs table)
- joao.html: tabs Documentos + Inbox (aprovações unificadas) + Autonomia (6 zonas)
- Telegram: /docs + /pendentes + /council
- `bin/incidents_tick.py`: check_documents() — idempotent per-doc incidents (kind=doc_expired_{id})
- `bin/radar_twin_bridge.py`: _ensure_doc_requests() — auto-checklist for each new tender
- Seed: 4 companies (parceiros), 2 persons, 3 vehicles, knowledge base (5 decisões chave)

## Twin Core + Factory v1 (estado: 2026-03-06)
- Tabelas: `twin_entities`, `twin_relations`, `twin_workflows`, `twin_cases`, `twin_tasks`, `twin_approvals`, `twin_documents`, `twin_document_templates`, `twin_document_requests`
- Workflow `cable_batch`: agendado→chegou→em_processamento→separacao→concluido→pronto_levantar→faturado→**fechado**
- Telegram bot: `bin/telegram_bot.py` | systemd: `aios-telegram.service` (Restart=always)
- Bot commands: /status, /approvals, /joao, /aprovar <id>, /rejeitar <id>, /control, /do, /lote, /help
- /lote subcommands: novo, ver, avancar, resultado, faturar, fechar
- Dashboards: `/ops` (NOC) + `/factory` (fábrica) + `/joao` (Chief of Staff) + `/control` (TV wall) + `/tenders` (TED) + `/finance` (financeiro)
- Faturação flow: `/lote resultado <id> <cobre> <plastico>` → `/lote faturar <id> <preco_kg>` → approval via /approvals → `/lote fechar`
- AUTH em server.js: OPS_TOKEN (/etc/aios.env) para escrita; AUTH_EXEMPT para leitura NOC/factory
- noc_query.py commands: twin_cases, twin_cable_batch_create, twin_batch_get, twin_batch_advance, twin_batch_resultado, twin_batch_faturar, twin_batch_faturar_ok, twin_factory_stats, twin_tenders, twin_tender_update, agent_suggestions, agent_briefing, agent_suggestion_read, decision_create

## Sprint H — Finance MVP (completo 2026-03-06)
- Tabelas: `finance_obligations`, `finance_payouts`, `event_timesheets`
- Dashboard: `ui/finance.html` — tabs: Obrigações, Pagamentos, Toconline/Rascunhos
- `cmd_payout_mark_paid`: cascade → timesheets `approved`→`paid` (mesmo worker + week_start)
- Toconline health: `GET /api/finance/toconline/health` (AUTH_EXEMPT) — verifica `~/.toc_token.json` expirado
- **Pendente:** token Toconline expirado — relogin em toconline.pt + copiar novo token

## Sprint J — Chief of Staff (completo 2026-03-06)
- Tabela: `agent_suggestions` (id, kind, title, details, ref_kind, ref_id, score, is_read, created_at)
  - SQL: `sql/310_agent_suggestions.sql`
- Script: `bin/joao_agent.py` (psycopg2, DSN: aios_user:jdl@127.0.0.1:5432/aios)
  - Ciclos: morning (briefing+sugestões), midday (alertas), evening (fecho dia)
  - Regras: MAX_NEW_TASKS_PER_DAY=3, dedupe 48h, score≥7 para sugerir
  - `twin_tasks` requer FK `case_id` — agente NÃO cria tarefas standalone, só `agent_suggestions`
- Timers: `aios-joao-morning.timer` (08:30), `aios-joao-midday.timer` (13:00 Mon-Fri), `aios-joao-evening.timer` (18:00)
- Telegram: `/joao` lista sugestões com inline keyboard (✅ aprovar / ❌ rejeitar / ↻ actualizar)
  - `joao:approve:id` → marca lida + cria entrada decision_queue
  - `/aprovar <id>` e `/rejeitar <id>` como texto também funcionam

## Sprint K — Control Room (completo 2026-03-06)
- `ui/control.html` — TV wall 6 zonas, dark theme, 12s auto-refresh, F11 fullscreen
- `/api/control/overview` — agregador público: tenders, workers, payouts, obligations, cases, suggestions, health, incidents, bank_unmatched
- joao.html: Quick Capture card (+ Ideia, + Decisão, + Tarefa), badges sidebar com alertas

## Sprint L — Reconciliação Bancária (completo 2026-03-06)
- Tabelas: `bank_transactions` (unmatched/matched/ignored) + `bank_reconciliation` (match_type, confidence)
- `bin/reconcile.py` — parse_csv (formatos PT BPI/CGD/Millennium), auto_match (valor+ref+NIF, confiança 60-95%), CLI commands
- noc_query.py: bank_transactions, bank_reconcile, bank_match, bank_ignore
- server.js: GET /api/finance/bank/transactions, POST /api/finance/bank/import|reconcile|match|ignore
- finance.html: tab "Reconciliação Bancária" — upload CSV (text/csv directo), auto-reconciliar, match manual, ignorar

## Sprint M — Incidentes (completo 2026-03-06)
- Tabela: `incidents` (source, kind, severity info/warn/crit, status open/resolved, dedupe 4h)
- `bin/incidents_tick.py` — detecta: workers offline >5min, tasks >24h, obrigações vencidas/<5d, tenders <3d, Toconline, bank >72h
- Timer: `aios-incidents.timer` (cada 5min)
- noc_query.py: incident_list, incident_resolve, incident_create
- server.js: GET /api/incidents, POST /api/incidents/:id/resolve

## Sprint N — Control Room Avançado (completo 2026-03-06)
- Semáforos: zone border-color (verde/amarelo/vermelho) por incidentes; header badge [N CRIT] piscante
- SRC_ZONE map: workers→ops, tasks→projects, finance/bank→finance, tender→radar, infra→infra
- Sound alerts (Web Audio API, botão 🔇/🔊)
- `?wall=1` — wall rotation: 3 vistas (overview/finance-focus/ops-focus) a cada 30s com slide animation
- Big timer para próxima obrigação fiscal (modo finance-focus)
- Botão Resolver incidentes inline (requer JWT)

## N4 — Portal Cliente + N5 — Radar TED (estado: 2026-03-05)
- N4: cliente_token em entity metadata → `/lote/:token` (público) → `ui/cliente.html`
  - API: `GET /api/twin/batch/by-token/:token` (AUTH_EXEMPT)
- N5: `bin/radar_ted.py` — TED API fetch + score + store + Telegram alert
  - Timer: `aios-radar.timer` (diário 07:00)
  - API: `GET /api/twin/tenders`, `POST /api/twin/tender/:id/estado`
  - Dashboards: `/tenders` (tenders.html), card em ops.html
  - DB: twin_entities(type='tender') + twin_cases(workflow_key='tender_intake')
  - Workflow 'tender_intake' inserido em twin_workflows
  - Pitfall: /twin/tender path bypasses JWT middleware via `req.path.startsWith('/twin/tender')`
  - TED API: `POST https://api.ted.europa.eu/v3/notices/search`, country=`PRT`, scope=`ALL`, paginationMode=`ITERATION`
  - TED fields válidos: notice-type, contract-nature, deadline-receipt-tender-date-lot, framework-maximum-value-lot, description-glo
  - `contract-title` NÃO existe no TED v3 — título gerado como `{nature} — {pub_num}` ou de description-glo
  - Queries com acentos PT causam 400 → usar ASCII (ex: "residuos" não "resíduos")

## NOC /ops (Express UI :3000)
- ops.html com sparklines + botões controlo + workers table
- AUTH_EXEMPT: syshealth, telemetry, backlog/recent, jobs/recent, watchdog/events, workers, actions/*
- Tabela `workers` + `aios-worker-heartbeat.timer` (30s) → `bin/worker_heartbeat.py`
- DB credentials: `aios_user:jdl@127.0.0.1:5432/aios` (corrigido de aios_db/aios2026)
- Ver roadmap completo: memory/aios_roadmap.md

## Ficheiros de memória do projecto (visão + negócio)
Sempre que o utilizador falar de negócio, produtos, verticais ou estratégia — ler e actualizar estes ficheiros:

- `memory/aios_roadmap.md` — estado técnico actual, infra, tokens, timers, pitfalls operacionais
- `memory/aios_vision.md` — 9 camadas de evolução, estado %, visão produto, ordem de prioridade
- `memory/aios_blueprint.md` — blueprint completo: 10 blocos, verticais (RH, Imobiliário, Fábrica), arquitectura duas instâncias, modelos de negócio

**Regra:** cada vez que o utilizador partilhar novas ideias, análises ou decisões sobre o projecto → guardar no ficheiro mais relevante, sem duplicar.

### Verticais de produto definidos
1. **RH** — 1€/func/mês, multi-tenant, funcionalidades: presenças, férias, dashboard gestor
2. **Imobiliário** — 30-150€/mês, geração automática CPCV + documentos, assinatura digital
3. **Fábrica Granulação Cabos** — app interna + SaaS para recicladores; agenda por kg/slot, tracking lotes, integração LME cobre, marketplace fase 2
4. **Eventos, Inventário, Manutenção** — previstos, blueprints a criar

### Decisão arquitectural chave
Duas instâncias do mesmo core: `ops` (interno, fábrica + RH + operações) e `saas` (multi-tenant, clientes externos).
Começar com uma (ops). Separar quando houver 1 cliente SaaS real.

## Python path rule
Never run Python services with `WorkingDirectory` pointing to a dir containing `.py` files
that shadow stdlib modules. Use parent dir + module path (`-m package.module`).
