# AUTONOMIA — Execução Semi-Autónoma Controlada

## Visão Geral

O módulo de Autonomia gere o ciclo de vida de `worker_jobs` com guardrails, retry automático e supervisão humana para tarefas sensíveis.

## Estados de worker_jobs

```
[enqueue]
    │
    ├─ guardrail match? ──YES──► blocked_review ──► [approve] ──► queued
    │                                             └─► [reject] ──► failed
    │
    NO
    ▼
  queued ──► running ──► done
               │
               ├─ timeout (>15min) ──► failed ──► [retry_count < max_retries] ──► queued
               └─ error             ──► failed ──► (idem)
```

| Status | Descrição |
|---|---|
| `queued` | Aguarda worker disponível |
| `running` | Em execução num node |
| `done` | Concluído com sucesso |
| `failed` | Falhou (erro ou timeout) |
| `blocked_review` | Bloqueado por guardrail — aguarda aprovação humana |

## Guardrails

Definidos em `public.autonomia_guardrails`. Padrões activos por defeito:

| Kind | Pattern | Acção |
|---|---|---|
| shell | `rm -rf` | block |
| shell | `drop table` | block |
| shell | `truncate` | block |
| shell | `delete from` | block |
| shell | `mkfs` | block |
| shell | `shutdown` | block |
| automation | `rm -rf` | block |
| automation | `drop table` | block |
| * | `requires_approval` | block |

Para desactivar um guardrail: `UPDATE public.autonomia_guardrails SET active=false WHERE id=<id>;`

Para adicionar: `INSERT INTO public.autonomia_guardrails (kind, pattern, action, description) VALUES ('shell', 'meu_padrao', 'block', 'motivo');`

## Orchestrator

`bin/autonomia_orchestrator.py` — corre a cada 60s via systemd timer.

**Retry:** jobs `failed` com `retry_count < max_retries` e falha < 60min → `queued`, `retry_count++`

**Zombie detection:** jobs `running` há > 15min → `failed` com `result={"error":"timeout watchdog","zombie":true}`

**Stats:** métricas de estado (queued/running/done_24h/failed_24h/blocked_review) → journal

## Fluxo de Aprovação

1. Job enfileirado com payload suspeito → status=`blocked_review`
2. Badge vermelho aparece na sidebar `🤖 Autonomia` e no card "Blocked Review"
3. Operador vê tabela com kind + payload preview
4. Clica ✓ (approve) → status=`queued`, regista `approved_by`
5. Clica ✗ (reject) → status=`failed`, result=`{"error":"rejeitado pelo operador"}`

## Ficheiros

| Ficheiro | Papel |
|---|---|
| `sql/540_autonomia.sql` | Schema: approved_by, autonomia_guardrails, requires_review |
| `bin/autonomia_orchestrator.py` | Retry + zombie detection (timer 60s) |
| `workers/aios-autonomia-orchestrator.service` | Systemd oneshot |
| `workers/aios-autonomia-orchestrator.timer` | Trigger 60s |
| `bin/noc_query.py` | `_check_guardrails`, `autonomia_blocked/approve/reject` |
| `ui/server.js` | `/api/autonomia/blocked`, `/api/autonomia/jobs/:id/approve|reject` |
| `ui/joao.html` | Painel Autonomia enriquecido com active jobs, failures, heartbeat |

## Instalação

```bash
# 1. Aplicar SQL
cat sql/540_autonomia.sql | docker exec -i postgres psql -U aios_user -d aios

# 2. Instalar timer
sudo cp workers/aios-autonomia-orchestrator.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now aios-autonomia-orchestrator.timer

# 3. Verificar
sudo systemctl status aios-autonomia-orchestrator.timer
journalctl -u aios-autonomia-orchestrator.service -n 20

# 4. Restart UI
sudo systemctl restart aios-ui.service
```

## Rollback

```sql
ALTER TABLE public.worker_jobs DROP COLUMN IF EXISTS approved_by;
ALTER TABLE public.agent_inbox DROP COLUMN IF EXISTS requires_review;
DROP TABLE IF EXISTS public.autonomia_guardrails;
```

```bash
git checkout HEAD -- bin/noc_query.py ui/server.js ui/joao.html
sudo systemctl disable --now aios-autonomia-orchestrator.timer
sudo rm /etc/systemd/system/aios-autonomia-orchestrator.{service,timer}
sudo systemctl daemon-reload
```
