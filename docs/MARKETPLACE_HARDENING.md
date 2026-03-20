# Marketplace — Hardening

## Estado: activo (2026-03-20)

Isolado da Linha A (Mauro/Ryan/Oneway). Não toca em `event_timesheets`, `service_billing.py`,
`whatsapp_handler.py` nem `worker_contacts`.

---

## Tabelas

| Tabela | Descrição |
|---|---|
| `marketplace_jobs` | Vaga publicada. Status: `open → matched → closed / cancelled` |
| `marketplace_applications` | Candidatura de worker. Status: `invited → accepted/declined → selected/rejected/expired` |
| `marketplace_worker_profiles` | Perfil marketplace do worker (WhatsApp, roles, zonas, rating) |

Quando o primeiro worker é `selected`, cria-se automaticamente um `service_jobs` (bridge) e um
`job_assignments` — ligando o marketplace ao fluxo de planeamento normal.

---

## Endpoints

| Método | Rota | Função |
|---|---|---|
| GET | `/api/admin/marketplace/jobs` | Listar vagas (filtro por status) |
| POST | `/api/admin/marketplace/jobs` | Criar vaga |
| GET | `/api/admin/marketplace/jobs/:id` | Detalhe + applications |
| POST | `/api/admin/marketplace/jobs/:id/invite` | Convidar worker |
| POST | `/api/admin/marketplace/jobs/:id/close` | Fechar ou cancelar vaga |
| POST | `/api/admin/marketplace/applications/:id/select` | Seleccionar worker |
| POST | `/api/admin/marketplace/applications/:id/reject` | Rejeitar candidatura |

---

## Hardening implementado

### 1. Audit log — `public.events`
Todas as acções críticas escrevem para `public.events` (source=`marketplace`):

| kind | trigger |
|---|---|
| `marketplace_job_created` | POST /jobs |
| `marketplace_invite_sent` | POST /jobs/:id/invite |
| `marketplace_worker_selected` | POST /applications/:id/select |
| `marketplace_worker_rejected` | POST /applications/:id/reject |
| `marketplace_job_closed` | POST /jobs/:id/close (action=close) |
| `marketplace_job_cancelled` | POST /jobs/:id/close (action=cancel) |

Consulta: `SELECT * FROM public.events WHERE source='marketplace' ORDER BY ts DESC;`

### 2. Idempotência

- **Double invite**: retorna `409 worker já convidado para este job`
  (verifica `status NOT IN ('declined','rejected','expired')`)
- **Double select**: retorna `400 status é selected` (aplicação já foi marcada)
- **Duplicate assignment**: `job_assignments` duplicado é ignorado (reutiliza o existente)
- **Duplicate service_job bridge**: protegido por `FOR UPDATE` na transacção (ver secção 3)

### 3. Race condition no bridge (service_job)

O endpoint `select` usa uma transacção completa com `FOR UPDATE`:

```
BEGIN
  SELECT ... FROM marketplace_applications FOR UPDATE OF ma   ← lock application
  SELECT service_job_id FROM marketplace_jobs ... FOR UPDATE   ← lock job row
  INSERT service_jobs (se service_job_id=NULL)
  UPDATE marketplace_jobs SET service_job_id=...
  INSERT job_assignments (com check de duplicado)
  UPDATE marketplace_applications SET status='selected'
  UPDATE marketplace_jobs SET status='matched' (se needed_workers atingido)
COMMIT
```

Dois `select` concorrentes: o segundo bloqueia até o primeiro fazer COMMIT, depois vê
`service_job_id` já preenchido e reutiliza.

### 4. Guards de validação

| Acção | Guard |
|---|---|
| Invite | Job deve ter `status='open'` (bloqueia matched/closed/cancelled) |
| Select | Job não pode estar `matched/closed/cancelled` |
| Select | Vagas preenchidas: bloqueia se `selected >= needed_workers` |
| Select | Worker deve ter `status IN ('invited','accepted')` |
| Close | Idempotente: retorna 409 se já fechado/cancelado |
| Cancel | Expira automaticamente todas as applications `invited`/`accepted` |

---

## Casos limite testados

```bash
# Setup
JWT=...  # joao / test123

# 1. Double invite → 409
curl -X POST .../jobs/2/invite -d '{"worker_id":1}'  # → ok
curl -X POST .../jobs/2/invite -d '{"worker_id":1}'  # → 409 "worker já convidado"

# 2. Select → job matched; re-invite → 400
curl -X POST .../applications/3/select               # → ok, status=matched
curl -X POST .../jobs/2/invite -d '{"worker_id":3}'  # → 400 "job não está aberto (status=matched)"

# 3. Double select → 400
curl -X POST .../applications/3/select               # → 400 "status é selected"

# 4. Cancel → invitations expired; double cancel → 409
curl -X POST .../jobs/3/close -d '{"action":"cancel"}'  # → ok, status=cancelled
curl -X POST .../jobs/3/close -d '{"action":"cancel"}'  # → 409 "job já fechado/cancelado"
SELECT status FROM marketplace_applications WHERE job_id=3;  # → expired
```

---

## Rollback / revert

O marketplace não altera tabelas da Linha A. Para remover completamente:

```sql
-- Remover bridge assignments criados pelo marketplace (marcados como confirmed via marketplace)
-- (não há flag directo — consultar marketplace_jobs.service_job_id para identificar)

-- Remover dados marketplace (CASCADE apaga applications)
TRUNCATE public.marketplace_jobs CASCADE;
TRUNCATE public.marketplace_worker_profiles;

-- Remover tabelas (irreversível)
DROP TABLE public.marketplace_applications;
DROP TABLE public.marketplace_jobs;
DROP TABLE public.marketplace_worker_profiles;
```

Endpoints em `server.js` podem ser removidos/desactivados sem impacto nas rotas existentes.
