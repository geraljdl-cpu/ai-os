# Validação Enhanced — Documentação

## Estado: activo (2026-03-20)

Hardening completo do fluxo de validação de serviço. Retrocompatível com todos os
registos de produção existentes.

---

## Estados explícitos (`validation_status`)

| Estado | Condição | DB `status` equivalente |
|---|---|---|
| `pending_validation` | `status=submitted` e token não expirado | `submitted` |
| `validated` | Aprovado; sem `needs_revalidation` | `approved_client`, `adjusted_client`, `invoiced_mock` |
| `rejected` | Rejeitado | `rejected_client`, `rejected` |
| `expired` | `status=submitted` e `token_expires_at < now()` | `submitted` (mas expirado) |
| `needs_review` | Validado + `needs_revalidation=true` | qualquer estado validado |

O campo `validation_status` é **computado** — não existe como coluna no DB. É calculado
por `_compute_validation_status()` e devolvido pelo GET `/api/service/validate/:token`.

---

## Novas colunas em `event_timesheets`

| Coluna | Tipo | Descrição |
|---|---|---|
| `token_expires_at` | TIMESTAMPTZ | Expiração do token (default: `created_at + 30d`) |
| `token_used_at` | TIMESTAMPTZ | Timestamp da primeira validação (approved ou rejected) |
| `rejection_reason` | TEXT | Motivo de rejeição obrigatório |
| `needs_revalidation` | BOOLEAN | Flag: alteração material após validação |
| `revalidation_reason` | TEXT | Motivo da revalidação (preenchido pelo operator) |

---

## Endpoints

### GET `/api/service/validate/:token` (público)
Retorna dados completos do registo + `validation_status` computado + expenses.

**Resposta quando expired:**
```json
{ "validation_status": "expired", "token_expires_at": "...", ... }
```

### POST `/api/service/validate/:token` (público)
Valida o registo.

**Novos campos no body:**
```json
{
  "approved": false,
  "rejection_reason": "Motivo obrigatório se rejected (min 10 chars no UI)",
  "note": "Nota opcional",
  "adjusted_days": 1.5,
  "extras": [{"description": "...", "amount": 25}],
  "expense_decisions": {"1": {"status": "approved_client"}}
}
```

**Guards:**
- `!approved && !rejection_reason` → 400 `Motivo de rejeição obrigatório`
- `validation_status === 'validated'` → 400 `Registo já validado`
- `validation_status === 'rejected'` → 400 `Registo já rejeitado`
- `validation_status === 'expired'` → 400 `Link de validação expirado (>30 dias)`

**Resposta:**
```json
{
  "ok": true,
  "ts_id": 46,
  "action": "rejected_client",
  "validation_status": "rejected",
  "effective_days": 1,
  "final_total": 123,
  "requires_review": false
}
```

### POST `/api/service/timesheets/:id/mark-revalidation` (operator auth)
Marca registo validado para revalidação (alteração material posterior).

```json
{ "reason": "Horas corrigidas manualmente após validação" }
```

Resposta: `{"ok": true, "ts_id": 44, "needs_revalidation": true}`

---

## Auditoria em `public.events` (source='service_billing')

| kind | trigger |
|---|---|
| `service_log_validation_attempt` | Tentativa bloqueada (already_validated, already_rejected, token_expired) |
| `service_log_validated` | Resultado final — inclui `validation_status`, `rejection_reason`, `remote_ip`, `requires_review`, `review_flags` |
| `service_log_revalidation_marked` | Operator marca para revalidação |

Consulta de auditoria:
```sql
SELECT kind, data->>'validation_status', data->>'rejection_reason', ts
FROM public.events
WHERE source='service_billing'
ORDER BY ts DESC LIMIT 20;
```

---

## `needs_review` automático

Definido automaticamente quando:
- Hora de validação fora de 06:00–23:00 UTC → `review_flags = ["validação fora de horário (Xh UTC)"]`
- Horas trabalhadas > 16h → `review_flags = ["horas elevadas (Xh)"]`

Behavior: **informativo**. Registo `requires_review=true` + flag no audit. Não bloqueia o fluxo.

---

## Token expiry

- Novos tokens: `NOW() + INTERVAL '30 days'` (definido em `submit_service_log()`)
- Backfill aplicado: tokens `submitted` existentes → `created_at + 30d`
- Backfill tokens já processados: `COALESCE(validated_at, created_at) + 1 second` (irrelevante, já processados)
- Index: `idx_et_token_expires` (apenas registos `submitted`)

---

## Atomicidade — race condition

`validate_service_log()` usa `SELECT ... FOR UPDATE OF ts` antes de qualquer UPDATE.
Dois requests simultâneos com o mesmo token ficam serializados:
- O primeiro acquires lock, faz UPDATE + COMMIT
- O segundo acquires lock, vê `validation_status=validated`, lança ValueError → 400

---

## UI — validar.html

### Estados renderizados
| `validation_status` | UI |
|---|---|
| `pending_validation` | Formulário completo com ajuste de dias, extras, despesas |
| `expired` | Banner vermelho "⏰ Link expirado" + contacto |
| `validated` | Banner verde "✅ Já validado" + data de validação |
| `rejected` | Banner vermelho "❌ Já rejeitado" + motivo (se preenchido) |
| `needs_review` | Banner amarelo "🔍 Em revisão" + motivo de revalidação |

### Rejeição obrigatória
- Textarea "Motivo de rejeição *" visível abaixo do botão Aprovar
- Botão "❌ Rejeitar" **desactivado** até `rejection_reason.trim().length >= 10`
- Guard adicional em `submitValidation()` (dupla verificação client-side)
- Guard em server.js (400 se vazio)
- Guard em service_billing.py (ValueError se vazio)

---

## Testes reproduzíveis

```bash
# Setup
TOKEN_NEW=$(python3 bin/service_billing.py submit 2 2026-05-01 8 Lisboa 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")

# 1. GET → pending_validation
curl -s "http://localhost:3000/api/service/validate/$TOKEN_NEW" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['validation_status'])"
# → pending_validation

# 2. POST rejeição sem motivo → 400
curl -s -X POST "http://localhost:3000/api/service/validate/$TOKEN_NEW" \
  -H "Content-Type: application/json" -d '{"approved":false}'
# → {"ok":false,"error":"Motivo de rejeição obrigatório"}

# 3. POST aprovação → ok
curl -s -X POST "http://localhost:3000/api/service/validate/$TOKEN_NEW" \
  -H "Content-Type: application/json" -d '{"approved":true}'
# → {"ok":true,"validation_status":"validated",...}

# 4. Replay → clean error
curl -s -X POST "http://localhost:3000/api/service/validate/$TOKEN_NEW" \
  -H "Content-Type: application/json" -d '{"approved":true}'
# → {"ok":false,"error":"Registo já validado"}

# 5. Token expirado (forçar via SQL)
TOKEN_EXP=$(python3 bin/service_billing.py submit 2 2026-05-02 8 Lisboa 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
docker exec -i postgres psql -U aios_user -d aios -c \
  "UPDATE event_timesheets SET token_expires_at=NOW()-INTERVAL '1 day' WHERE validation_token='$TOKEN_EXP'"
curl -s "http://localhost:3000/api/service/validate/$TOKEN_EXP" | python3 -c \
  "import sys,json; print(json.load(sys.stdin)['validation_status'])"
# → expired
curl -s -X POST "http://localhost:3000/api/service/validate/$TOKEN_EXP" \
  -H "Content-Type: application/json" -d '{"approved":true}'
# → {"ok":false,"error":"Link de validação expirado (>30 dias)"}

# 6. mark-revalidation
JWT=...
APPROVED_ID=$(docker exec -i postgres psql -U aios_user -d aios -tAc \
  "SELECT id FROM event_timesheets WHERE status IN ('approved_client','invoiced_mock') LIMIT 1")
curl -s -X POST "http://localhost:3000/api/service/timesheets/$APPROVED_ID/mark-revalidation" \
  -H "Authorization: Bearer $JWT" -H "Content-Type: application/json" \
  -d '{"reason":"Horas corrigidas manualmente"}'
# → {"ok":true,"ts_id":...,"needs_revalidation":true}

# 7. Rejeição com motivo
TOKEN_REJ=$(python3 bin/service_billing.py submit 2 2026-05-03 8 Lisboa 2>/dev/null \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])")
curl -s -X POST "http://localhost:3000/api/service/validate/$TOKEN_REJ" \
  -H "Content-Type: application/json" \
  -d '{"approved":false,"rejection_reason":"Horário registado incorretamente foram 6h nao 8h"}'
# → {"ok":true,"validation_status":"rejected",...}
docker exec -i postgres psql -U aios_user -d aios -c \
  "SELECT rejection_reason, token_used_at FROM event_timesheets WHERE validation_token='$TOKEN_REJ'"
# → mostra rejection_reason preenchido e token_used_at definido

# 8. Audit log
docker exec -i postgres psql -U aios_user -d aios -c \
  "SELECT kind, data->>'validation_status', data->>'rejection_reason', ts
   FROM public.events WHERE source='service_billing' ORDER BY ts DESC LIMIT 8;"
```

---

## Rollback

```sql
-- Remover novas colunas (CUIDADO: perde dados de rejection_reason, etc.)
ALTER TABLE public.event_timesheets
    DROP COLUMN IF EXISTS token_expires_at,
    DROP COLUMN IF EXISTS token_used_at,
    DROP COLUMN IF EXISTS rejection_reason,
    DROP COLUMN IF EXISTS needs_revalidation,
    DROP COLUMN IF EXISTS revalidation_reason;
DROP INDEX IF EXISTS idx_et_token_expires;
```

Reverter código: `git checkout <commit_anterior> -- bin/service_billing.py ui/server.js ui/validar.html`

---

## Ficheiros modificados

| Ficheiro | Tipo | Descrição |
|---|---|---|
| `sql/510_validation_hardening.sql` | CRIADO | 5 novas colunas + backfill + index |
| `bin/service_billing.py` | MODIFICADO | `_compute_validation_status`, guards, FOR UPDATE, rejection_reason, mark_revalidation |
| `ui/server.js` | MODIFICADO | rejection_reason guard, clean error parsing, mark-revalidation route |
| `ui/validar.html` | MODIFICADO | validation_status UI, estados explícitos, rejection reason textarea |
| `docs/VALIDATION_ENHANCED.md` | CRIADO | Esta documentação |
