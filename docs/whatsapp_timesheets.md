# WhatsApp Timesheets — Ponto via Twilio

## Visão Geral

O colaborador pica o ponto directamente pelo WhatsApp. O sistema calcula as horas, gera payout e envia automaticamente o link de validação ao cliente.

```
Worker WhatsApp → Twilio → AI-OS webhook → DB
                                         ↓
                                   Cliente recebe link
                                         ↓
                               https://aios.grupojdl.pt/validar/<token>
                                         ↓
                              approved → payout + invoice draft
```

---

## Setup Twilio Sandbox

### 1. Criar conta Twilio (gratuito)
https://www.twilio.com/try-twilio

### 2. Ativar WhatsApp Sandbox
Dashboard → Messaging → Try it out → Send a WhatsApp message
- Sandbox number: `+1 415 523 8886`
- **Join code: `join victory-spring`**
- Workers enviam esse código para `+1 415 523 8886` no WhatsApp

### 3. Configurar Webhook
Em Twilio Console → Messaging → Settings → WhatsApp Sandbox Settings:
- **When a message comes in:** `https://aios.grupojdl.pt/api/whatsapp/inbound` → POST
- **Status callback URL:** `https://aios.grupojdl.pt/api/whatsapp/status` → POST (opcional)

### 4. Variáveis de ambiente
Adicionar em `/home/jdl/.env.db`:
```
TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_AUTH_TOKEN=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TWILIO_WHATSAPP_NUMBER=+14155238886
DEFAULT_CLIENT_WHATSAPP=+351XXXXXXXXX
AIOS_PUBLIC_BASE=https://aios.grupojdl.pt
```

### 5. Reiniciar serviço UI
```bash
sudo systemctl restart aios-ui.service
```

### 6. Verificar
```bash
curl https://aios.grupojdl.pt/api/whatsapp/health
# → {"ok":true,"configured":true,"number":"+14155238886"}
```

---

## Registar Colaborador

Antes de usar, cada worker precisa de se registar:

**Worker envia para o número Twilio:**
```
registo João Silva
```
**Sistema responde:**
```
✅ Registado como João Silva.
Podes usar:
• inicio <local> — entrada
• fim — saída
• estado — ver turno
```

Ou o admin insere directamente na DB:
```sql
INSERT INTO public.worker_contacts (worker_name, whatsapp_phone, people_id, default_client_phone)
VALUES ('João Silva', '+351910123456', 1, '+351960000000');
```

---

## Comandos WhatsApp

| Comando | Descrição | Exemplo |
|---------|-----------|---------|
| `inicio <local>` | Abre turno | `inicio Lisboa` |
| `inicio <local> carro` | Abre com carro (+10€/dia) | `inicio Porto carro` |
| `fim` | Fecha turno, calcula, envia ao cliente | `fim` |
| `estado` | Ver turno actual / último registo | `estado` |
| `registo <nome>` | Registar número no sistema | `registo João Silva` |
| `ajuda` | Lista comandos | `ajuda` |

---

## Fluxo Completo

```
1. Worker → "inicio Lisboa"
   Sistema → "✅ Entrada registada: 09:32 📍 Lisboa"
   DB: event_timesheets status='active', start_time=NOW()

2. Worker → [localização GPS WhatsApp]
   Sistema → "📍 Localização guardada — Rua Augusta, Lisboa"
   DB: gps_lat, gps_lon, gps_source='whatsapp_gps'

3. Worker → "fim"
   Sistema → "✅ Saída: 18:05 | 8.5h → 1 dia | Payout: 50€"
   DB: check_out_at, hours, status='submitted', validation_token=<uuid>
   Cliente recebe WhatsApp:
     "📋 Serviço registado
      👤 João Silva
      📅 16/03/2026 | 09:32→18:05 (8.5h)
      📍 Lisboa
      💶 Total: 123.00€
      Validar: https://aios.grupojdl.pt/validar/<token>"

4. Cliente abre link, aprova
   DB: status='approved'
   finance_payouts: +50€
   twin_invoices: SRV-2026-XXXX draft 123€
```

---

## Regras de Faturação

| Horas | Dias equiv. | Worker | Cliente (s/ IVA) | Cliente (c/ IVA 23%) |
|-------|-------------|--------|-----------------|---------------------|
| ≤ 12h | 1.0 dia | 50€ | 100€ | 123€ |
| > 12h | 1.5 dias | 75€ | 150€ | 184.50€ |
| > 16h | 2.0 dias | 100€ | 200€ | 246€ |
| + carro | +10€/dia | add | — | — |

Parametrizável via tabela `service_rates` (row `name='standard'`).

---

## Base de Dados

### Novos campos em `event_timesheets`
| Campo | Tipo | Descrição |
|-------|------|-----------|
| `worker_phone` | TEXT | Número WhatsApp do worker |
| `client_phone` | TEXT | Número do cliente (para notificação) |
| `check_out_at` | TIMESTAMPTZ | Timestamp do `fim` |
| `gps_lat` | NUMERIC(10,7) | Latitude |
| `gps_lon` | NUMERIC(10,7) | Longitude |
| `gps_source` | TEXT | `whatsapp_gps` ou `manual` |

`start_time` = check-in · `check_out_at` = check-out · `status='active'` = turno aberto

### Tabela `worker_contacts`
| Campo | Descrição |
|-------|-----------|
| `whatsapp_phone` | +351XXXXXXXXX (UNIQUE) |
| `people_id` | FK para `persons.id` |
| `default_client_phone` | Cliente para notificação automática |
| `active` | FALSE = desactivado |

---

## Ficheiros

| Ficheiro | Descrição |
|---------|-----------|
| `bin/whatsapp_handler.py` | Handler principal (chamado pelo server.js) |
| `bin/test_whatsapp.py` | Teste local (sem Twilio real) |
| `sql/460_whatsapp_schema.sql` | Migração DB |
| `ui/server.js` | Rotas `POST /api/whatsapp/inbound`, `/status`, `GET /health` |
| `ui/validar.html` | Página de validação (cliente) |
| `runtime/whatsapp/handler.log` | Logs do handler |

---

## Migração Sandbox → Produção

O código é **agnóstico ao número**. Não há hardcode do sandbox nem do join code.
A única diferença entre sandbox e produção é a configuração em `.env.db`.

### Passo a passo para produção

**1. Ligar o número da empresa ao Twilio**
- Twilio Console → Phone Numbers → Manage → Buy a number
- OU ligar número existente via "Bring Your Own Number"
- Ativar WhatsApp Business no número (requer aprovação Meta 24-72h)

**2. Atualizar `.env.db`**
```bash
TWILIO_WHATSAPP_FROM=+351XXXXXXXXX   # número oficial da empresa
WHATSAPP_MODE=production
```

**3. Reiniciar serviço**
```bash
sudo systemctl restart aios-ui.service
```

**4. Verificar**
```bash
curl https://aios.grupojdl.pt/api/whatsapp/health
# → {"ok":true,"configured":true,"mode":"production","from":"+351XXXXXXXXX"}
```

Sem refatoração de código. Sem tocar no handler. Só `.env.db` + restart.

---

## Adicionar colaborador autorizado

Só números registados em `worker_contacts` podem usar o sistema.

```sql
INSERT INTO public.worker_contacts (worker_name, whatsapp_phone, people_id, default_client_phone)
VALUES ('Nome Colaborador', '+351XXXXXXXXX', <people_id>, '+351XXXXXXXXX_CLIENTE');
```

Ou via WhatsApp (o próprio colaborador):
```
registo Nome Colaborador
```
(Depois um admin confirma que `people_id` está correcto em `worker_contacts`.)

---

## Privacidade

| Informação | Worker vê | Cliente vê |
|-----------|-----------|-----------|
| Horas trabalhadas | ✅ | ✅ |
| Local | ✅ | ✅ |
| Valor a receber (payout) | ✅ | ❌ |
| Carro (+10€) | ✅ (linha separada) | ❌ |
| Valor a pagar (fatura) | ❌ | ✅ |
| Link de validação | ❌ | ✅ |

---

## Troubleshooting

**Mensagem não chega ao worker:**
- Verificar `runtime/whatsapp/handler.log`
- Confirmar que o número enviou `join victory-spring` (sandbox)
- Verificar `TWILIO_ACCOUNT_SID` e `TWILIO_AUTH_TOKEN` em `.env.db`

**Worker não autorizado:**
- Ver log: `grep UNAUTHORIZED runtime/whatsapp/handler.log`
- Registar: `INSERT INTO worker_contacts ...`

**Status de entrega das mensagens:**
- Ver `runtime/whatsapp/status.log` (JSON por linha)

**Debug últimas mensagens:**
```bash
tail -50 /home/jdl/ai-os/runtime/whatsapp/handler.log
```

Multi-cliente: adicionar `default_client_phone` por worker em `worker_contacts`.
