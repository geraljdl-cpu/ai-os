# AI-OS — Runbook Mínimo

> Referência rápida de operações. Ver ARCHITECTURE.md para contexto completo.

---

## Estado do Sistema

```bash
# Worker em execução?
systemctl --user status aios-worker

# Logs em tempo real (filtra ruído idle)
journalctl --user -u aios-worker -f

# Snapshot completo (backlog + jobs + tamanho runtime)
cd ~/ai-os && python3 bin/system_snapshot.py

# Métricas do worker (última vez que correu, estado)
python3 bin/worker_metrics.py

# Relatório das últimas falhas
python3 bin/fail_report.py
```

---

## Backlog de Tasks

```bash
# Ver todas as tasks (pending/done/failed)
cd ~/ai-os && python3 bin/backlog_pg.py list

# Contagens por estado
python3 bin/backlog_pg.py status

# Adicionar task manualmente
bin/aiosctl queue add "Título" "Goal descritivo para o agent"

# Próxima task a executar
python3 bin/backlog_pg.py next
```

---

## Forçar Autofill (quando backlog vazio)

```bash
cd ~/ai-os && python3 - <<'EOF'
from bin import backlog_pg
n = backlog_pg.autofill_if_empty(4)
print(f"Criadas: {n} tasks housekeeping")
EOF
```

> O autofill só actua se não houver tasks pending.
> Cria 4 tarefas seguras: aiosctl status, heartbeat, README, cleanup jobs.

---

## Aprovar/Rejeitar Tools com Gate de Aprovação

```bash
# Listar aprovações pendentes
python3 bin/approval_pg.py list

# Aprovar
python3 bin/approval_pg.py approve <id>

# Rejeitar
python3 bin/approval_pg.py reject <id>
```

---

## Controlo do Worker

```bash
# Parar
systemctl --user stop aios-worker

# Iniciar
systemctl --user start aios-worker

# Reiniciar (após alteração de código)
systemctl --user restart aios-worker

# Desactivar início automático
systemctl --user disable aios-worker
```

---

## Backup e Restore

```bash
# Fazer backup agora (inclui teste de restore)
cd ~/ai-os && python3 bin/backup_pg.py backup

# Ver backups existentes
python3 bin/backup_pg.py status

# Restaurar backup específico (cria DB de teste isolada)
python3 bin/backup_pg.py restore backups/aios_db_YYYY-MM-DD_HH-MM-SS.sql.gz

# Limpar backups com mais de 30 dias
python3 bin/backup_pg.py cleanup
```

---

## Secrets

```bash
# Listar secrets configurados
cd ~/ai-os && python3 bin/secrets.py list

# Obter valor
python3 bin/secrets.py get DATABASE_URL

# Definir novo secret
python3 bin/secrets.py set TELEGRAM_TOKEN <valor>

# Importar ficheiro .env
python3 bin/secrets.py import-env ~/.env.db
```

---

## Alerting Telegram

```bash
# Configurar (uma vez)
cd ~/ai-os
python3 bin/secrets.py set TELEGRAM_TOKEN <token-do-botfather>
python3 bin/secrets.py set TELEGRAM_CHAT_ID <chat-id>

# Testar
python3 bin/alerting.py test

# Enviar alerta manual
python3 bin/alerting.py send custom "Mensagem de teste"
```

---

## HTTPS via Caddy

```bash
# Iniciar Caddy (porta 8443)
cd ~/ai-os && ./bin/caddy run --config Caddyfile.aios --adapter caddyfile &

# Ou via systemd
systemctl --user start aios-caddy

# Testar
curl -k https://localhost:8443/api/auth/me -H "Authorization: Bearer <token>"
```

---

## Login API

```bash
curl -s -X POST http://localhost:3000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"aios2026"}' | python3 -m json.tool
```

---

## Diagnóstico Rápido

```bash
cd ~/ai-os

# Postgres acessível?
python3 -c "from bin import db; s=db.SessionLocal(); s.execute(__import__('sqlalchemy').text('SELECT 1')); print('DB OK'); s.close()"

# Agent-router acessível?
curl -s http://127.0.0.1:5679/health

# Tools engine funciona?
echo '{"steps":[{"tool":"bash_safe","input":{"cmd":"echo ok"}}]}' \
  | python3 bin/tools_engine.py 2>/dev/null | python3 -m json.tool
```
