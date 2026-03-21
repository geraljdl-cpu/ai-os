# AI-OS — Blueprint Completo de Evolução

## Nível 0 — Base existente
Núcleo operacional completo: Postgres, API, jobs distribuídos, timers, watchdog, telemetria,
alertas, backups + push cloud, NOC /ops, tokens (worker + ops), autopilot gates+merge, UI systemd.

---

## 10 Blocos para o objetivo final

### 1 — Interface humana (tu falares com o sistema)
**1.1 Telegram "Command & Control"**
- Bot oficial AI-OS: /do, /plan, /status, /approve
- Resposta com: resumo + ações propostas + botões aprovar/rejeitar/reexecutar

**1.2 WhatsApp**
- Ponte inicial via Telegram (validar produto)
- WhatsApp Business API depois (Twilio/Meta): compliance, opt-in, templates, custos/conversa

**1.3 App nativa** (mais tarde, quando: autenticação sólida + multi-tenant + billing + auditoria)

### 2 — Identidade, permissões e auditoria
- JWT existe mas falta: gestão users/roles (admin, operator, client), refresh tokens, revogação
- Policy engine: "quem pode fazer o quê" por ação
- Auditoria: log de quem pediu + o que foi executado + resultado (obrigatório para clientes)

### 3 — Multi-tenant (transforma isto numa empresa)
- tenant_id em todas as tabelas (backlog, jobs, events, telemetry, assets, configs)
- Isolamento: row-level mínimo; schema por tenant mais profissional
- Config por cliente: tokens, webhooks, canais, limites, retenção, branding

### 4 — Agentes (o "cérebro")
**Council de agentes:**
- Planner (plano e milestones)
- Builder (gera código)
- Critic/QA (tenta destruir e encontrar falhas)
- Operator (executa ações seguras e deployments)
- Researcher (contexto externo)

**Protocolo:** mote → plano + riscos + custo operacional → gate/approval → execução

**Memória operacional:** decisões passadas, configs por cliente, runbooks, padrões de falha

### 5 — Motor de criação de software
**Templates de produto (blueprints repetíveis):**
- App Reservas + pagamentos + painel admin
- App Inventário + picking + QR + histórico
- Dashboard NOC/telemetria para empresas
- Gestão de manutenção (tickets, SLA, checklists)
- Gestão de eventos (riders, staff, mapas, cronogramas)

**Pipeline de geração:**
criar projeto → gerar backend → gerar frontend web → gerar app mobile → testar → deploy → criar tenant → gerar docs

**CI/CD obrigatório:** builds automáticos, gates, deploy staging/produção, rollback

### 6 — Execução segura
- Classificação: read-only | safe actions | privileged | destructive (bloqueado)
- Approval humano para privileged/destructive: /approve ou janela NOC + log auditável
- Sandboxes: staging separado de produção

### 7 — Operação industrial (correr semanas sem intervenção)
**Observabilidade:** métricas persistentes longas, alertas com severidade/dedupe, dashboards históricos
→ Evolução natural: Prometheus + Grafana (mesmo single-node)

**Runbooks e auto-recovery:**
- "se A falha → faz B"
- root cause nos eventos, contador de flaps, circuit breaker (não reiniciar infinito)

**Housekeeping e retenção por categoria:**
- telemetry: N dias | events: N dias | jobs: N dias | backups: 30 dias cloud (feito)

### 8 — Storage
**Cloud (começado):** push artefactos (builds, reports), push media pesada (VJ loops, assets), rclone crypt opcional

**NAS (quando hardware chegar):**
- Partilhas: /media, /backups, /models, /datasets, /clients
- Regra: C: e disco servidor só para sistema; dados pesados no NAS/cloud

### 9 — Apps nativas (Google Play / App Store)
- Flutter (1 codebase iOS+Android): login + tenant + offline cache + push notifications
- Android: Play Console, signing | iOS: Apple Developer Program, TestFlight
- O robô gera código e builds; conta e publicação são processos humanos

### 10 — Modelo de negócio
**Produtos:**
- Dashboard + alertas + automação (mensal)
- App reservas + painel (mensal + taxa)
- Operação eventos (projeto + retainer)
- Manutenção/indústria (mensal por unidade)

**Entrega repetível:** onboarding automático (criar tenant, configurar, importar dados), templates por setor, SLAs

**Confiança:** auditoria, logs, backups, segurança, privacidade

---

## Bloco H — Finance & Compliance Control (PRIORIDADE ALTA)

### Objetivo
Guardar financeiro da empresa: não falhar IVA, SS, AT, fornecedores, RH, obrigações pessoais.
AI-OS como **guardião financeiro e fiscal** — memória externa + controlador de risco + gestor de obrigações.
Especialmente crítico com PHDA: elimina "sabia que tinha de pagar → adiei → perdi o fio → multa".

### Fluxo central
```
obrigação → calendário fiscal → alerta antecipado → valor → aprovação → pagamento → comprovativo → registo Twin
```

### 3 Níveis de proteção
- **H1 — Lembrar:** alertas 10d / 5d / 2d / dia-de
- **H2 — Preparar:** valor + entidade + referência + origem + documento + sugestão pagamento
- **H3 — Confirmar:** "João, IVA de X€ pronto para aprovação" → aprovas → registo → confirma saída banco

### Módulos
1. **Fiscal Calendar Engine** — obrigações empresa + pessoais, IVA, SS, retenções, AT, datas, janela de alerta
2. **Finance Inbox** — dashboard: a pagar hoje / próximos 7d / atrasados / à espera aprovação / comprovativos em falta
3. **Approval Layer** — nada sai sem controlo; deteta → calcula → avisa → aprova → segue
4. **Bank Connector** — ler saldo, movimentos, confirmar pagamento, reconciliar com faturas
5. **AT / SS / Contabilidade Connectors** — Toconline, AT, SS, email parsing de avisos e guias

### Etiquetas
- `business`: IVA, SS empresa, salários, fornecedores, clientes
- `personal`: AT pessoal, SS pessoal, pagamentos recorrentes

### Tabelas
- `finance_obligations` — calendário e obrigações
- `finance_payments` — pagamentos efetuados
- `finance_accounts` — contas bancárias
- `finance_approvals` — aprovações de saída
- `finance_reconciliation` — matching obrigações ↔ movimentos banco
- `finance_documents` — comprovativos

### Dashboards
- `/finance` — painel principal
- `/finance/obligations` — calendário fiscal
- `/finance/payouts` — pagamentos
- `/finance/reconciliation` — reconciliação bancária

### Plano 7 blocos (ordem executável, foco dia-a-dia)

**B1 — Calendário Fiscal**
- tabela `finance_obligations` (type, entity, due_date, amount, status, source)
- seeds: IVA mensal/trimestral, SS empresa, SS independente, retenções, IRS/IRC
- alertas: 10d / 5d / 2d / dia-de → Telegram
- dashboard `/finance`

**B2 — Finance Inbox**
- rota `/finance`, secções: a pagar hoje / próximos 7d / atrasados / à espera aprovação / pagos recentemente
- tabelas: `finance_obligations` + `finance_approvals` + `finance_payments`

**B3 — Timesheets de Eventos (RH)**
- Worker App: Evento → start → stop → horas → notas
- tabela `event_timesheets` (worker_id, event_id, start, end, hours, status)

**B4 — Validação Cliente**
- portal `/client/event/:token` — cliente vê horas por worker, botão "aprovar"
- `timesheet.status = approved`

**B5 — Fatura Automática (Toconline)**
- trigger: timesheets approved → API Toconline → cria fatura → guarda PDF
- tabela `twin_invoices` ligada ao case/event

**B6 — Pagamentos Semanais RH**
- script `payout_run` (todas as segundas) — calcula worker/horas/valor/total
- dashboard `/finance/payouts` + alerta Telegram com resumo semanal

**B7 — Ligação ao Banco**
- ler saldo + movimentos, confirmar pagamentos, reconciliar com faturas/obrigações/payouts
- tabela `finance_bank_transactions`

### Resultado final dos 7 blocos
```
worker trabalha → regista horas → cliente valida → fatura emitida → relatório RH → pagamentos
IVA/SS/AT → calendário → alerta → aprovação → pagamento → comprovativo
```

### Rotina diária automática (PHDA)
Todos os dias de manhã o AI-OS envia resumo com:
- tarefas críticas, pagamentos próximos, decisões pendentes, eventos do dia

### Ordem de implementação
- **H1** — obrigações + alertas + tasks automáticas + approvals (MVP imediato)
- **H2** — eventos RH + faturação + mapa semanal pagamentos
- **H3** — banco + confirmação pagamentos + matching obrigações/faturas
- **H4** — automação fiscal séria: Toconline + documentos + comprovativos + reconciliação automática

---

## Ordem certa (single-node, sem hardware extra)
1. **Telegram como interface oficial** (tu falas com o sistema) ← próximo passo
2. Council de agentes (planner/builder/critic/operator)
3. Templates de produto + pipeline de geração
4. Multi-tenant + billing básico
5. CI/CD + staging/produção
6. NAS (quando chegar hardware) para media/artefactos
7. Apps móveis Flutter (quando 1-2 produtos a render)

## Próximo passo imediato de alto impacto
**"AI-OS no Telegram"** — bot + endpoints + auth + comandos + job queue com approvals

---

## Caso de uso: App RH (primeiro produto comercial)

### Porquê RH como primeira app
- Todas as empresas precisam
- Recorrente (subscrição mensal)
- Não depende de hardware
- Sem comissão Apple/Google se uso interno
- Escala fácil com multi-tenant já planeado

### Funcionalidades V1 (só o que vende)
- Login
- Cadastro de funcionários (dados, documentos, contratos)
- Presenças (entrada/saída, turnos, horas extra, faltas)
- Férias (pedido, aprovação, saldo automático)
- Dashboard gestor (quem trabalha, férias, faltas, horas)

### Funcionalidades que aumentam preço
- Check-in com GPS
- QR code no local de trabalho
- Relatórios automáticos (horas/mês)
- Exportação para contabilidade
- Alertas automáticos (atrasos, horas extra)

### Modelo de negócio
- 1€ / funcionário / mês
- 50 funcionários = 50€/mês por cliente
- 10 empresas médias = 1000€/mês

### Custos de infra
- Google Play: 25$ (uma vez)
- Apple Developer: 99$/ano
- Servidor: já existe (custo quase zero)

### Arquitectura
App Android/iPhone → API (AI-OS) → Postgres → Dashboard web
Cada empresa: espaço isolado (multi-tenant)

### Evolução V2+
Avaliação desempenho, gestão formação, assinatura digital, integração salários, integração ERP

### Plataforma de apps empresariais
App 1: RH | App 2: Inventário | App 3: Eventos | App 4: Manutenção | App 5: Imobiliária
Tudo ligado ao mesmo AI-OS core.

---

## Caso de uso: App Imobiliária (vertical 2)

### Problema
Processo imobiliário é manual e fragmentado: documentos, PDFs, emails, validações, erros.
Cada processo consome horas a mediadores, advogados e notários.

### Fluxo automatizado
novo imóvel → upload documentos → validação automática → geração documentos → gestão processo → escritura

### Documentos gerados automaticamente
- **Legais:** CPCV, procuração, minuta escritura, declaração de venda
- **Fiscais:** caderneta predial, certidão permanente, IMI, identificação fiscal
- **Bancários:** dossier crédito habitação, comprovativos, avaliação bancária
- **Mediação:** contrato mediação, ficha técnica habitação, ficha cliente

### Etapas do processo automatizado
1. Angariação: ficha imóvel + contrato mediação
2. Comprador: ficha cliente + proposta compra
3. CPCV: gerado automaticamente (cláusulas, valores)
4. Banco: dossier completo organizado
5. Escritura: documentos finais + checklist

### Funcionalidades chave
- Checklist automático (documentos em falta)
- Alertas (documento expirado, data escritura)
- Geração PDF com botão (ex: "Gerar CPCV")
- Assinatura digital no telemóvel
- AI deteta incoerências (ex: área caderneta ≠ certidão → alerta)

### Modelo de negócio
- 30€/mês por mediador individual
- 150€/mês por agência
- 5€/processo (por venda)

### Porque escala
Cada mediador tem 10-20 processos ativos. Automatizar vale dinheiro imediato.

---

## Caso de uso: App Fábrica de Granulação de Cabos (vertical 3)

### Contexto
Liga AI + indústria real + receita física. AI-OS não gere só software — gere operações físicas.

### Processo da fábrica
receção cabos → pesagem → separação por tipo → granulação → separação cobre/plástico → pesagem final → stock → venda cobre

### Módulos da app
- **Receção:** fornecedor, tipo cabo, peso, data → cria lote
- **Gestão lotes:** estado (por processar / em produção / concluído)
- **Produção:** lote entra na máquina → resultado (ex: cobre 1200kg, plástico 800kg)
- **Stock:** cobre, plástico, resíduos em tempo real
- **Vendas:** cliente, peso, preço/kg → cálculo de lucro automático

### Inteligência AI-OS
- Previsão de preço: ligar ao LME copper price → sugerir quando vender
- Eficiência da máquina: kg processados / tempo / energia → métricas
- Análise de lotes: tipo de cabo X → rendimento cobre Y% → escolha de fornecedores

### Automação industrial (futuro)
Sensores / PLC: sensor máquina ligada, sensor peso, contador produção → AI-OS em tempo real

### Modelo de negócio
- App interna para fábrica própria
- OU vendida a recicladores (mercado sem software bom para isto)

### Visão ERP modular
AI-OS → RH + Fábrica + Imobiliário + Eventos + Inventário + Apps clientes = ERP modular criado por ti

---

## Módulo: Departamento Administrativo (Concursos Públicos)

### Problema
Cada candidatura exige: certidões (SS, Finanças, registo criminal, certidão permanente), declarações modelo, anexos preenchidos, comprovativos. Horas de trabalho administrativo repetido.

### Regra base
Automação prepara tudo → humano valida e autoriza (responsabilidade legal mantida)

### Fluxo
novo concurso → analisa requisitos → lista documentos → vai buscar certidões → preenche modelos → compila dossier → notifica humano → humano revê e autoriza

### Base de dados de documentos
Cada documento: tipo, data emissão, data validade, link.
Alerta automático quando certidão expira em <5 dias → obter nova certidão

### Checklist automático por concurso
entidade, prazo, documentos exigidos → gera checklist: ✓ registo criminal ✓ certidão finanças ✓ certidão SS ✓ declaração modelo ✓ proposta técnica

### Preenchimento automático de modelos
Campos repetidos (nome empresa, NIF, morada, representante legal) preenchidos automaticamente nos anexos PDF/Word

### Output final
pasta concurso: certidões + declarações + proposta + anexos preenchidos → PDF compilado

### Inteligência futura
Identificar concursos relevantes, avaliar probabilidade de ganhar, sugerir candidatura com base no histórico da empresa

### Resultado operacional
1 pessoa administrativa + sistema prepara 80% → pessoa só valida, assina, envia

### Plataformas de concursos públicos em Portugal — estado real das APIs

| Plataforma | API pública |
|------------|-------------|
| Vortal | ⚠️ API privada enterprise (VORTAL Connect — requer contrato) |
| AnoGov | ❌ só interface web (login + certificado digital) |
| AcinGov | ❌ sem API pública |
| SaphetyGov | ⚠️ integração enterprise (ERP/e-invoicing) |
| Gatewit | ❌ sem API pública |

### Estratégia de integração realista
- **Camada 1 (radar público):** TED Europa, BaseGov, Diário da República → dados estruturados, públicos
- **Camada 2 (plataformas):** login automatizado → lista concursos → download PDF
- **Camada 3 (IA):** extrai requisitos → gera checklist → prepara candidatura

### BaseGov — ponto de entrada ideal
Base central pública de contratos públicos portugueses. Reúne concursos de várias plataformas, dados estruturados, acesso público. Melhor ponto de entrada para o radar.

### Tender Intelligence System — Radar Nacional (arquitectura técnica)

**Duas camadas:**

**Camada 1 — Radar (fontes abertas):**
- **BASE.gov.pt** — centraliza contratos PT, dados abertos; recolher anúncios/CPV/entidades → criar `tender entities` no Twin
- **TED (UE)** — API oficial para contratos acima de limiares EU; filtrar por país PT, CPV, setor
- **Diário da República** — fonte legal/publicação para cruzamento

Output: tabela `tenders_inbox` com: título, entidade, CPV, valor, prazo, links, fonte, score

**Camada 2 — Enriquecimento por plataforma (quando passa no filtro):**
- Credenciais da empresa → sessão autenticada → download PDFs → armazenar → extrair requisitos com IA
- VORTAL Connect (enterprise) quando houver volume/parceria
- Nota: respeitar T&C de cada plataforma

**Motor IA do radar:**
- **Scoring 0-100:** fit com perfil (CPV, localização, valor, prazos, histórico ganhos/perdas)
- **Extração de requisitos do PDF:** documentos obrigatórios, requisitos técnicos, critérios adjudicação, prazos, garantias
- **Checklist automática:** cria case/workflow no Twin com tasks (obter certidão X, preencher anexo Y, aprovação)
- **Dossier:** compila anexos + monta pasta final + cria approval para submissão

**Interface Telegram:**
"Novo concurso relevante (score 82). Queres analisar?" → botões: Analisar / Ignorar / Preparar candidatura / Revisão humana

**O que falta no AI-OS para o radar funcionar:**
1. Ingestores BASE + TED + DR com schedule diário
2. Normalização para `tender entities`
3. Scoring + filtros (CPV, keywords, geografia, thresholds)
4. Pipeline enrichment: download peças + parsing + checklist
5. Workflow de candidatura + approvals

**Ordem de implementação:**
1. TED API + BASE (mais estável, maior retorno)
2. Integração plataformas via login (só para concursos que já passaram no filtro)
3. Template library de anexos e declarações (ganho gigante)

**Decisões técnicas confirmadas:**
- BASE tem atraso e nem sempre tem as peças — útil para radar, não para peças completas
- TED mais completo para contratos acima de limiares EU; BASE é única fonte para ajuste directo/concurso simples
- PDFs de concursos PT frequentemente são scans → pipeline OCR + LLM necessário antes de parsing estruturado (custo computacional não trivial a escala)
- **Não construir o radar antes do Twin core** — sem entities/cases/approvals é só um script que envia emails
- Ordem obrigatória: `twin core` → `radar v1` → enriquecimento por plataforma

---

## Digital Twin — Arquitectura Técnica Completa

### Definição operacional
Grafo de entidades e relações, com estado actual e histórico. Tudo vira:
- entidade (cliente, lote, contrato, concurso, evento)
- estado (agendado, em execução, concluído, falhou)
- evento (criado, aprovado, pago, assinado, entregue)

### Duas instâncias do Twin
- **OPS (interno):** fábrica, eventos, equipa, operação
- **SAAS (clientes):** RH, imobiliário, outras apps vendidas
- Estrutura idêntica, dados separados

### Modelo de dados mínimo (Postgres)

**Core (obrigatório):**
- `tenants`, `users`, `roles`, `user_roles`, `sessions`
- `policies`, `policy_bindings` (quem pode fazer o quê)
- `audit_log` (append-only, todas as acções relevantes)

**Twin graph (entidades e relações):**
- `entities` — id, tenant_id, type, name, status, metadata(jsonb), created_at, updated_at
  - types: client, project, batch, machine, event, tender, property, employee, asset, invoice
- `relations` — from_entity_id, to_entity_id, rel_type, metadata(jsonb)
  - exemplos: client→batch, batch→machine, tender→document, property→owner

**Eventos (coração do "vivo"):**
- `events` — ts, level, source, kind, entity_id, message, data(jsonb)
  - (já existe; evolução: garantir entity_id e data útil)

**Workflows (processos repetíveis):**
- `workflows` — key, name, version, definition(jsonb)
- `cases` — id, tenant_id, workflow_key, entity_id, status, sla_due_at, data(jsonb)
- `tasks` — id, case_id, title, type(human/auto), status, assignee, due_at, payload(jsonb)
- `approvals` — id, case_id, action, status(pending/approved/rejected), requested_by, approved_by, context(jsonb)

**Documentos:**
- `document_templates` — key, version, format(docx/pdf/html), content, variables_schema(jsonb)
- `documents` — id, tenant_id, entity_id, template_key, status(draft/final/signed), storage_uri, hash, metadata(jsonb)
- `document_requests` — "preciso de certidão X" / "pedir ao cliente upload Y" — status, due_at, channel

### Event-sourcing light
- estado actual nas tabelas (entities/cases)
- tudo que muda estado gera event com detalhe
- regra: "sem evento, não aconteceu"

### Workflows por módulo

**Fábrica Cabos:**
pedido → aprovação → confirmado → check-in → processamento → pronto → entregue → faturado
- capacidade kg/h por máquina; slot = kg_estimado / capacidade + buffer
- preço/kg "congelado" no booking
- notificação ao cliente em cada mudança de estado

**Imobiliário:**
angariação → documentação imóvel → proposta → CPCV → banco → escritura → fecho

**RH:**
leave_request: pedido → aprovação → saldo actualizado
onboarding: criar user, documentos, políticas

**Concursos públicos:**
análise requisitos → checklist docs → obter certidões → preencher anexos → compilar dossier → aprovação → submissão → resposta

### Agentes (pipeline multi-agente com gates)
- **Planner** — transforma pedido em plano + tarefas
- **Document Agent** — gera/compila docs a partir de templates
- **Ops Agent** — executa acções seguras (jobs)
- **QA/Critic** — encontra falhas e inconsistências
- **Compliance Agent** — regras (concursos, RGPD, assinaturas)
- **Creative Agent** — renders, imagens, vídeo, anúncios
- **Finance Agent** — pricing, invoices, margens (com limites)
- Regra: agentes NUNCA executam privileged sem approval

### Tipos de execução (control plane)
- `read` — queries, relatórios
- `safe_action` — healthcheck, enqueue, gerar PDF, criar rascunho
- `privileged_action` — deploy, restart, submissão, assinatura, pagamentos (iniciar) → requerem approval
- `destructive` — bloqueado por policy por defeito

### Interfaces
- **Tu:** Telegram bot (/status /approve /do /plan) + NOC /ops + brief diário opcional 09:00
- **Equipa interna:** Portal web OPS (lotes, agenda, tarefas, documentos) + permissões por função
- **Clientes:** Portal web (tracking, docs, faturas) + App móvel Flutter (fase 2)

### Roadmap de execução (sem hardware extra)
1. **Twin core** — entities + relations + cases + tasks + approvals + documents + NOC mostra approvals pendentes
2. **Telegram** — bot com /status /approve /do + botões + qualquer privileged passa por approvals
3. **Fábrica Cabos** — booking por kg + tracking + faturação (ROI imediato interno)
4. **Primeiro SaaS** — RH ou Imobiliário (multi-tenant, billing)
5. **Criativo/Render** — job type render, storage assets, entrega automática

### Padrão de produto (cada módulo tem sempre)
tabelas próprias + workflow(s) + templates + endpoints + dashboards + alertas

---

## Arquitetura de duas instâncias (decisão arquitectural)

### Camada base comum
- Código core (API, jobs, agents, gates, NOC, backups)
- Biblioteca de módulos (RH, Fábrica, Imobiliário, etc.)
- Sistema de blueprints de apps
- CI/CD com dois targets: `ops` (interno) e `saas` (clientes)

### Instância 1 — AI-OS Interno (Operações)
- Finalidade: gerir os teus negócios
- Acesso restrito (equipa interna)
- Integrações físicas (máquinas, sensores, produção)
- Pode correr on-premise + cloud backup
- Módulos: Fábrica granulação, RH interno, Gestão clientes B2B, Financeiro básico, Dashboards

### Instância 2 — AI-OS Plataforma (SaaS)
- Finalidade: vender software como serviço
- Multi-tenant, billing, planos
- App web + apps mobile
- Infra cloud separada da operação interna
- Módulos por vertical: RH, Imobiliário, futuro: inventário, manutenção

### Separação técnica
- Monorepo (core partilhado)
- Deploy targets: `ops` / `saas`
- DBs separadas, tokens separados, segredos separados

### Fluxo de criação de produtos
ideia → blueprint (tabelas + APIs + ecrãs) → gerar backend + frontend → deploy (interno ou SaaS) → evolução via agents

### Próximos passos práticos
1. Primeiro módulo SaaS (RH ou Imobiliário)
2. Módulo interno Fábrica Cabos (agenda por kg + tracking)
3. Sistema de blueprints para gerar apps rapidamente

---

### App Fábrica — modelo detalhado (serviço a clientes externos)

**Conceito:** cliente externo traz cabos à fábrica → reserva slot de máquina → tracking em tempo real → fatura automática

**Fluxo de agendamento:**
cliente → seleciona data/hora → indica kg estimados → sistema calcula tempo máquina → slot reservado

**Cálculo de capacidade:** capacidade linha (ex: 1200 kg/h) → cliente indica kg → sistema reserva horas exatas → evita filas/conflitos

**Estados do lote:**
AGENDADO → CHEGOU → EM PROCESSAMENTO → SEPARAÇÃO → CONCLUÍDO → PRONTO A LEVANTAR

**Durante processamento (visível ao cliente):**
- peso processado em tempo real
- tempo restante estimado
- notificação push quando pronto

**Integração mercado:** ligar LME/COMEX → mostrar cobre recuperado estimado × preço mercado → valor estimado do lote

**Faturação automática:** peso processado × preço serviço (ex: 0,40€/kg) → relatório lote + fatura + histórico cliente

**Evolução fase 2 — marketplace:**
cliente processa → app calcula cobre recuperado → mostra compradores → cliente vende diretamente → tu ganhas comissão/fee

---

## Mapa de Entidades do Digital Twin (2026-03-06)

### Regra base
- `entity` = objeto real | `case` = processo/assunto | `task` = ação | `approval` = decisão formal | `event` = histórico

### Tipos de entities

**Pessoas:** `person`, `worker`, `partner`, `client_contact`
**Organizações:** `company`, `client`, `supplier`, `partner_company`, `public_entity`
**Operação:** `event`, `project`, `job`, `service_order`, `batch`, `tender`
**Ativos:** `machine`, `vehicle`, `site`, `warehouse`, `equipment`
**Financeiro:** `invoice`, `payment`, `obligation`, `payout`, `bank_account`
**Conhecimento/pipeline:** `idea`, `proposal`, `document`, `workflow_template`

### twin_links — grafo empresarial
```sql
twin_links(id, from_entity_id, to_entity_id, link_type, created_at)
```
Tipos: `client_of`, `contact_of`, `assigned_to`, `works_on`, `belongs_to`, `generated_from`, `related_to`, `paid_by`, `approved_by`, `located_at`, `uses_equipment`

Exemplo:
```
worker João --works_on--> event Festival X
Festival X --client_of--> Empresa Y
invoice #55 --generated_from--> Festival X
```

### Tipos de cases
`tender_case`, `event_case`, `project_case`, `maintenance_case`, `finance_case`, `compliance_case`, `factory_batch_case`, `idea_case`

### Famílias de tasks (para filtrar em /worker, /ops, /joao)
`execution_task`, `analysis_task`, `approval_task`, `finance_task`, `compliance_task`, `maintenance_task`

### Tabela people (RH base)
```sql
people(id, name, role, cluster, hourly_rate, phone, email, active)
```
Clusters: `events`, `engineering`, `maintenance`, `finance`, `partners`

### Tabela clients
```sql
clients(id, company_name, nif, billing_email, contact_name, phone, notes)
```
Relações: cliente → muitos cases, contacts, invoices

### Regra de ouro
Tudo importante deixa: `entity/case update` + `event` + `task ou approval se necessário`. Sem evento, não aconteceu.

### Ordem de implementação sem caos
- **Fase 1:** people + clients + twin_links + financial entities
- **Fase 2:** ideas + projects + event_timesheets + payouts
- **Fase 3:** machines + sites + maintenance logs

---

## Sprint H — Finance + RH dentro da arquitectura Twin (próximo)

**Princípio:** Finance e RH não são tabelas soltas — são entities + cases + tasks + links dentro do Twin.

### H1 — Calendário fiscal + alertas
- `entity(type=obligation)` para cada obrigação fiscal
- `case(type=compliance_case)` por prazo
- tasks automáticas: "aprovar pagamento"
- alertas 10d/5d/2d/dia Telegram

### H2 — RH eventos (timesheets)
- `people` table + link `worker --works_on--> event`
- `event_timesheets(worker_id, event_id, start, end, hours, status)`
- portal cliente valida → `approval` → `invoice` gerada

### H3 — Pagamentos semanais
- script `payout_run` (segundas) usa `people.hourly_rate` + `event_timesheets`
- `entity(type=payout)` + link `payout --paid_by--> bank_account`
- alerta Telegram com resumo

### H4 — Bank connector
- `entity(type=bank_account)` + `finance_bank_transactions`
- matching obrigações ↔ movimentos → `twin_links(generated_from)`
