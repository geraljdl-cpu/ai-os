# AI-OS Roadmap & Objetivo Final

## Estado atual — sessão 2026-03-04 (completo)

### PONTOS CONCLUÍDOS
1. ✅ Alertas (Telegram + severidade + anti-spam + auditoria DB)
2. ✅ Telemetria persistente (tabela `telemetry`, timer 10s, `/api/telemetry/history`)
3. ✅ Scheduler distribuído v1 (tabela `worker_jobs`, pull model, testado com LPTDD99)
4. ✅ Autopilot completo (ciclo fechado: task→patch→gates→reflect→merge)
5. ✅ NOC visual + Hardening (ops.html + endpoints NOC + AUTH_EXEMPT + retenção)
6. ✅ Hardening v2: token por worker + OPS token + worktree-dirty guard + enqueue fix
7. ✅ Cloud backup: pg_dump push para jdlaicloud:AI-OS-Backups/pgbackups (timer 03:15)
8. ✅ Estabilização UI: PM2 eliminado, systemd único gestor, pm2-jdl.service disabled

### Ponto 5 (depois do 4): NOC visual avançado
Gráficos históricos reais (usar `/api/telemetry/history`), cluster map, timeline eventos

---

## Infraestrutura base (tudo funcional)

### Servidor: DESKTOP-CPLTTV3 (100.121.255.36)
- Docker: agent-router:5679, agent-core:8010, postgres, redis, ollama
- UI gerida por **PM2** (não systemd!) — `pm2 restart aios-ui --update-env`
- NOC: http://localhost:3000/ops
- DB: `aios_user:jdl@127.0.0.1:5432/aios`

### Cliente cluster: DESKTOP-LPTDD99 (100.95.160.84)
- Heartbeat: PowerShell loop `irm $url` a cada 30s
- Worker runner: `C:\aios\worker_runner.ps1` (pull jobs do servidor)
- Rede: Tailscale mesh

### Systemd timers ativos
- `aios-autopilot.timer` — 30s
- `aios-watchdog.timer` — 60s
- `aios-worker-heartbeat.timer` — 30s (usa HTTP, não Python)
- `aios-pgbackup.timer` — diário (OnCalendar=daily → meia-noite)
- `aios-pgbackup-push.timer` — diário 03:15 (push para jdlaicloud:AI-OS-Backups/pgbackups)
- `aios-alerts.timer` — 30s
- `aios-telemetry.timer` — 10s

### DB tables (Postgres)
- `tasks` — backlog (pending/done/failed/skipped/cancelled)
- `workers` — heartbeat cluster
- `worker_jobs` — scheduler distribuído (queued/running/done/failed)
- `events` — auditoria de alertas
- `alert_state` — anti-spam (cooldown warn=5min, crit=1min)
- `telemetry` — histórico CPU/RAM/disk/load/backlog (10s)
- `users`, `roles`, `audit_log`, `jobs`, `steps`, `approvals`

### API endpoints — autenticação
- **Públicos (AUTH_EXEMPT):** syshealth, telemetry, backlog/recent, jobs/recent, watchdog/events, workers, workers/register
- **Worker token** (X-AIOS-WORKER-ID + X-AIOS-WORKER-TOKEN): `/api/worker_jobs/lease`, `/api/worker_jobs/report`
- **OPS token** (X-AIOS-OPS-TOKEN em `/etc/aios.env`): `/api/worker_jobs/enqueue`
- **JWT** (Authorization: Bearer): todas as outras (actions/tick, actions/watchdog, approve, etc.)

### Tokens (2026-03-04)
- DESKTOP-CPLTTV3-agent: `7b59a7b33c3df06a08a67cdba1a90ae606a3c28193936de55b3734cccebb39dd`
- DESKTOP-LPTDD99-agent: `d5fbc08f66dc1d1e3bdb83777c09a89091c1ae4d94e4968769f887db3bcebdf4`
- OPS token: em `/etc/aios.env` como AIOS_OPS_TOKEN (AIOS_OPS_TOKEN lido pelo server.js no startup)
- worker_runner.ps1 actualizado servido em `http://servidor:3000/worker_runner.ps1`

### Windows LPTDD99 — Scheduled Task
```powershell
schtasks /Create /TN "AIOS Worker Runner" /SC ONSTART /RL HIGHEST /TR "powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\aios\worker_runner.ps1" /F
schtasks /Run /TN "AIOS Worker Runner"
```

### Ficheiros chave — Ponto 5 (NOC + hardening)
- `bin/noc_query.py` — queries PG para NOC (telemetry, workers, events, syshealth, worker_jobs)
- `ui/ops.html` — dashboard NOC: charts Chart.js, workers table, events timeline, task queue, controlo
- AUTH_EXEMPT em server.js: NOC read + worker pull-model endpoints
- `/api/actions/*` requerem auth; `/api/maintenance/cleanup` para retenção de dados
- CUIDADO: autopilot timer corre a cada 30s com `git add -A` — pode commitar ficheiros WIP!
  Sempre commitar/stash antes de deixar ficheiros no worktree enquanto o timer está activo.

### Ficheiros chave — Ponto 4 (autopilot completo)
- `bin/autopilot_tick.py` — orquestrador principal (ponto 4). Timer aponta aqui.
- `bin/gates.sh` — validação: conflitos git, compileall bin/, smoke HTTP
- `bin/merge_if_clean.sh` — merge --no-ff controlado para main
- `bin/reflect_prompt.txt` — template reflexão (formato search/replace)
- Motor de patch: formato `<<<< FILE: ... >>>>` (search/replace, fallback unified diff)
- backlog_pg.py usa `db.py` / tabela `jobs` (não `public.tasks`!) + JSON fallback
- `peek_next_task_json(task_type=None)` para todos os tipos de task
- `compileall` só em `bin/` (runtime/ tem lixo de runs anteriores)

### Ficheiros chave
- `bin/alerts_tick.py` — alertas Telegram + auditoria
- `bin/telemetry_tick.py` — colector telemetria
- `bin/worker_heartbeat.py` — heartbeat (usa HTTP via curl no systemd)
- `bin/worker_runner.ps1` — worker runner Windows
- `ui/ops.html` — NOC panel
- `ui/server.js` — Express API
- `/etc/aios.env` — env vars para systemd (DB, Telegram)

### Pitfalls importantes
- UI gerida por PM2, NÃO systemd. Usar `pm2 restart aios-ui --update-env`
- PM2 não tem env vars por defeito → passar com `DATABASE_URL=... pm2 start ...`
- Orphan node na porta 3000: `sudo kill -9 $(ss -tlnp | grep 3000 | grep -oP 'pid=\K[0-9]+')`
- psycopg2 só instalado para user jdl, não root → systemd services usam HTTP ou sudo pip3
- report endpoint usa ficheiro temp /tmp/wjob_result_ID.json para evitar newlines no JSON

---

## Objetivo final: AI-OS como sistema operativo de empresa

### Arquitetura alvo
```
AI-OS
 ├ NOC (cabine de pilotagem — /ops)
 ├ Runtime de agentes especializados
 ├ Cluster de máquinas
 ├ Data layer (Postgres como memória operacional)
 └ Módulos de negócio (EventsOS, FactoryOS, BusinessOS)
```

### Tudo concluído (2026-03-04)
Sistema autónomo + observável + distribuído + hardening + cloud backup + estabilizado ✅

---

## Plano de Níveis — 2026-03-05

### NÍVEL 0 — Base técnica ✅ (completo)
Postgres, timers, watchdog, backup, NOC /ops, Telegram /do, workers distribuídos, auth

### NÍVEL 1 — Factory v1 ✅ (completo 2026-03-05)
- ✅ Twin Core (entities, cases, tasks, approvals, documents)
- ✅ Workflow cable_batch (8 estados: agendado→fechado)
- ✅ Telegram: /lote novo/ver/avancar/resultado/faturar/fechar
- ✅ Faturação com approval (Telegram inline keyboard → auto-advance)
- ✅ Dashboard /factory separado do /ops
- **Critério:** consegues correr lote completo só via Telegram + /factory, sem SQL

### NÍVEL H — Finance + RH + Compliance MVP ✅ (completo 2026-03-06)
**Objetivo:** fechar o primeiro ciclo financeiro real da empresa.
**Fluxo alvo:** evento → worker regista horas → cliente valida → fatura emitida → pagamentos RH calculados → IVA/SS/AT no dashboard

**Critério de sprint concluído:** ✅ FECHADO
worker regista horas → cliente aprova → fatura fica pronta → mapa semanal RH gerado → IVA/SS/AT aparecem com alertas

**Implementado (2026-03-06):**
- Tabelas: `people`, `clients`, `twin_links`, `finance_payouts` + seeds (3 técnicos + 1 cliente)
- `clients` tem token único por cliente (portal público)
- noc_query: `people_list`, `client_list`, `client_timesheets`, `client_timesheet_action`, `payout_list`, `payout_run`, `payout_mark_paid`, `obligation_approve`
- server.js: `GET /api/client/:token/timesheets`, `POST /api/client/:token/timesheet/:id/approve|reject`, `GET|POST /api/finance/payouts`, `GET /api/people`, `GET /api/clients`, `POST /api/twin/obligations/:id/approve`
- `bin/payout_run.py`: cálculo semanal (segunda-feira), Telegram, evento Twin
- client.html: secção timesheets com approve/reject por token
- finance.html: tab Pagamentos RH + approve obrigações + calcular semana
- finance_alerts.py: já existia (alertas 10d/5d/2d/0d)

#### H1 — Base de dados
Tabelas a criar: `people`, `clients`, `twin_links`, `event_timesheets`, `finance_obligations`, `finance_payments`, `finance_payouts`, `finance_documents`
- `people(id, name, role, cluster, hourly_rate, phone, email, active)` — clusters: events/engineering/maintenance/finance/partners
- `clients(id, company_name, nif, billing_email, contact_name, phone, notes)`
- `twin_links(id, from_entity_id, to_entity_id, link_type, created_at)`
- `event_timesheets(worker_id, event_id, start, end, hours, notes, status)` — status: draft/submitted/approved/paid

#### H2 — Timesheets no /worker
- ver eventos do dia, start/stop, nota, submeter horas
- eventos Twin: `worker_checkin`, `worker_checkout`, `timesheet_submitted`

#### H3 — Validação cliente no /client/:token
- lista técnicos + horas + notas + aprovar/rejeitar
- eventos Twin: `timesheet_approved`, `timesheet_rejected`

#### H4 — Faturação
- gerar documento interno pós-aprovação
- criar `entity(type=invoice)` ligado ao case/event/client via twin_links
- preparar integração Toconline
- eventos: `invoice_created`, `invoice_sent`, `invoice_paid`

#### H5 — Pagamentos semanais RH (todas as segundas)
- script `payout_run`: horas aprovadas não pagas × hourly_rate
- dashboard `/finance/payouts` + alerta Telegram
- eventos: `payout_run_created`, `payout_marked_paid`

#### H6 — Obrigações fiscais
- calendário: IVA, SS empresa, SS independente, AT, retenções, obrigações pessoais
- alertas Telegram: 10d / 5d / 2d / dia
- tabela `finance_obligations(type, entity, due_date, amount, status, source)`

#### H7 — Dashboard /finance
4 zonas: obrigações | faturação | pagamentos RH | alertas
Secções: a pagar hoje / próximos 7d / atrasados / à espera aprovação

**Ordem de implementação:**
1. people + clients + twin_links
2. event_timesheets
3. endpoints worker para horas
4. aprovação cliente
5. finance_obligations
6. /finance dashboard
7. finance_payouts
8. integração Toconline

**Depois do Sprint H:** Painel do João → Conselho de IA → Control room → Twin mais profundo

### NÍVEL I — Conselho de IA + Painel do João ✅ (completo 2026-03-06)

**Implementado:**
- Tabelas: `idea_threads`, `idea_messages`, `idea_reviews`, `decision_queue`
- `bin/idea_router.py` — 4 agentes Claude (strategist/engineering/operations/finance) + síntese automática
- `bin/joao_briefing.py` — briefing 08:30 + fecho 18:00 Telegram (só seg-sex)
- noc_query: `idea_create`, `idea_list`, `idea_get`, `idea_archive`, `idea_reviews`, `idea_create_case`, `decision_list`, `decision_resolve`
- server.js: `POST /api/ideas`, `GET /api/ideas`, `GET|POST /api/ideas/:id`, `POST /api/ideas/:id/analyze|create_case|archive`, `GET /api/ideas/:id/reviews`, `GET|POST /api/decisions`
- `ui/joao.html` — 6 secções: Hoje / Ideias / Decisões / Radar / Financeiro / Operação
- Telegram: `/idea <texto>` — cria thread, responde com ID
- Timers: `aios-joao-morning.timer` (08:30) + `aios-joao-evening.timer` (18:00)
- `ANTHROPIC_API_KEY` lida de /etc/aios.env pelo server.js e passada ao idea_router
- Modelo: claude-sonnet-4-6, 4 prompts distintos por papel
- Polling automático na UI a cada 5s até análise completa

**Fluxo completo:**
João escreve /idea → thread criada → UI /joao → Analisar → 4 agentes Claude → síntese → Criar projeto → twin_case + tasks

### NÍVEL J — Chief of Staff ✅ (completo 2026-03-06)
- `agent_suggestions` (kind, title, score, is_read, ref_kind, ref_id)
- `bin/joao_agent.py` — ciclos morning/midday/evening, dedupe 48h, MAX_NEW_TASKS=3, score≥7
- Timers: morning 08:30, midday 13:00 (Mon-Fri), evening 18:00
- Telegram: `/joao` + inline approve/reject, `/aprovar <id>`, `/rejeitar <id>`
- Dashboard: `/joao` (joao.html) com Quick Capture, badges alertas

### NÍVEL K — Control Room v1 ✅ (completo 2026-03-06)
- `ui/control.html` — TV wall 6 zonas, dark theme, 12s refresh, F11 fullscreen
- `/api/control/overview` — agregador público (AUTH_EXEMPT): tenders, workers, payouts, obligations, cases, suggestions, health
- Telegram `/control` — resumo rápido

### NÍVEL L — Reconciliação Bancária (PRÓXIMO)
**Objetivo:** ligar o dinheiro real do banco ao ERP.

**Fluxo:** CSV movimentos banco → match automático (valor+referência+NIF) → marcar invoice paid → evento

**Tabelas a criar:**
- `bank_transactions(id, date, amount, description, reference, nif, status, matched_invoice_id, imported_at)`
- `bank_reconciliation(id, transaction_id, invoice_id, match_type, confidence, created_at)`

**API:**
- `POST /api/finance/bank/import` — upload CSV + parse + store
- `GET /api/finance/bank/transactions` — lista com status (matched/unmatched/ignored)
- `POST /api/finance/bank/reconcile` — trigger auto-match
- `POST /api/finance/bank/match` — match manual (transaction_id + invoice_id)

**Dashboard:** `/finance` tab "Reconciliação" — movimentos vs faturas, pendentes de match, botão "reconciliar"

**Critério fechado:** movimento banco → invoice marcada como paid automaticamente

### NÍVEL M — Alarmes e Incidentes
**Objetivo:** Control Room com semáforos reais (verde/amarelo/vermelho).

**Tabelas:** `incidents(id, source, kind, severity, title, details, status, resolved_at, created_at)`

**Fontes automáticas:** workers offline >5min, tarefas bloqueadas >24h, obrigação fiscal <5d, tender urgente <3d, API externa falhou

**API:** `GET /api/incidents` (activos), `POST /api/incidents/:id/resolve`

**Visualização:** `/control` com cores por zona, contador activo no header

### NÍVEL N — Control Room Avançado
**Objetivo:** NOC real modo TV.

- `?wall=1` → wall rotation automático entre vistas (30s cada)
- Sound alert (opcional) quando novo incidente
- Big timers para obrigações fiscais urgentes
- Auto-layout responsivo para ecrã 4K/UHD

### NÍVEL O — AI Council v2
**Objetivo:** expandir de 4 para 7 agentes.

Novos agentes: Strategist, Engineering, Operations, Finance, Legal, Market, Risk
Adicionar: consensus_score, disagreement_detection por ideia

### NÍVEL P — Produto / Multi-tenant SaaS
**Objetivo:** transformar em produto vendável.

Arquitectura: `company_id` em todas as tabelas, isolamento, billing
Verticais-alvo: empresas eventos, PME industriais, câmaras, manutenção

---

## Estado v1.0 — ~70% concluído (2026-03-06)
```
✅ ERP (Finance MVP, Factory, Workers, Clients)
✅ Workflow engine (Twin Core, cable_batch, tender_intake)
✅ AI decision layer (Chief of Staff, AI Council 4 agentes)
✅ Radar oportunidades (TED/radar_ted)
✅ Control Room v1
🔲 Reconciliação bancária (Sprint L)
🔲 Incidentes com semáforos (Sprint M)
🔲 Control Room avançado (Sprint N)
🔲 AI Council v2 (Sprint O)
🔲 Multi-tenant SaaS (Sprint P)
```

### NÍVEL J-antigo — Painel do João (integrado em Sprint J acima, DONE)
- Rota `/joao` + `GET /api/joao/dashboard`
- 7 secções: Hoje / Ideias Rápidas / Radar / Operação / Dinheiro / Decisões Pendentes / Próximos 7 Dias
- Tabelas: `ideas(id, text, status, created_at)` + `decision_queue(id, type, ref_id, status)`
- API: `POST /api/ideas`, `GET /api/ideas?status=`, `POST /api/ideas/:id/create_case`
- Automação PHDA: resumo Telegram 08:30 (job `daily_briefing_0830`) + fecho 17:30 (`daily_closing_1730`)
- **"Primeiro dia de trabalho"**: flow completo acordas → Telegram → dashboards → tarefas do dia
- PHDA-friendly: máx. 3 tarefas críticas, máx. 5 decisões pendentes, números grandes

### NÍVEL AI — Conselho de IA (multi-agente)
- 4 agentes em paralelo por ideia capturada no Painel do João
  - AI Strategist (GPT) → visão, mercado, risco
  - AI Engineering (Claude) → soluções técnicas, viabilidade
  - AI Operations (Ollama local) → execução, equipa, recursos
  - AI Finance (GPT) → custos, receita, ROI
- Ficheiro: `bin/idea_router.py`
- Tabela: `idea_reviews(idea_id, agent, analysis, created_at)`
- Endpoints: `GET /api/ideas/:id/reviews`, `POST /api/ideas/:id/create_case`
- Fluxo final: `[ Analisar mais ] [ Criar Projeto ] [ Arquivar ]` → `idea → twin_case → tasks`
- Daily standup automático às 09:00: AI-OS gera briefing de equipa com tarefas + alertas + radar

### NÍVEL 2 — Permissões e multi-operador (PRÓXIMO)
- RBAC: admin / supervisor / operador / cliente
- /factory visível só para operador+
- approvals só para supervisor+
- audit trail por ação (quem fez o quê)

### NÍVEL 3 — Documentos (motor base)
- PDF Guia do Lote (check-in, pesos, resultado)
- PDF Fatura de Serviço
- Storage cloud + NAS futuro
- approval antes de emitir

### NÍVEL 4 — Portal Cliente
- booking de slot + kg estimado
- tracking em tempo real (estados, docs)
- notificações Telegram/WhatsApp/email
- histórico lotes + faturação

### NÍVEL 5 — Radar Nacional (BASE + TED)
- ingestão diária + dedupe + scoring
- entidade tender + case "analisar concurso"
- tasks automáticas (pedir certidões, extrair requisitos)
- alert Telegram quando score >X
- /tenders dashboard com funil

### NÍVEL 6 — Automação administrativa
- cofre certidões/declarações (validade, refresh)
- OCR + extração de PDFs
- approvals + "assinatura" humana

### NÍVEL 7 — Multi-tenant SaaS
- isolamento por cliente
- billing/planos
- ligar novo cliente em 1 dia

### NÍVEL 8 — AppSpec + Gerador
- scaffolder (endpoints, UI, permissões, workflows)
- catálogo de módulos reutilizáveis

### NÍVEL 9 — Apps nativas
- App Operações (interno)
- App Cliente (tracking + pedidos)
- push notifications + offline mode

### Hardware pendente
- NAS: 4-bay, TrueNAS SCALE ou Ubuntu+ZFS, 2.5GbE
- 10× Dell 3050 MT cluster (sem RAM/disco/CPU, €29/un) — decisão pendente compra
