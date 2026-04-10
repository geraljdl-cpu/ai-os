# Cluster Inventory — AI-OS
*Actualizado: 2026-04-10 (nodegpu integrado)*

## Topologia

```
                    ┌─────────────┐
                    │    node1    │  192.168.1.111
                    │ control-    │  scheduler · DB · UI
                    │  plane      │  agent-router · redis
                    └──────┬──────┘
                           │ LAN 192.168.1.0/24
        ┌──────────────────┼──────────────────┐
        │                  │                  │
  ┌─────┴──────┐    ┌──────┴──────┐    ┌──────┴──────┐
  │  nodecpu   │    │   nodegpu   │    │  node-nas   │
  │ cpu-main   │    │  gpu-main   │    │  storage    │
  │ 1.172      │    │  1.120      │    │  1.118      │
  │ preprocess │    │ gpu_infer.  │    │  NFS · bkp  │
  │ general    │    │ llm_gpu     │    │  models     │
  └────────────┘    └────────────┘    └─────────────┘
        │
  ┌─────┴──── Auxiliares ───────────────────────┐
  │ node2 1.112  ai_analysis                    │
  │ node4 1.122  radar · automation · ai_anal.  │
  │ node5 1.123  general (temporário)           │
  │ node6 1.124  general (temporário)           │
  │ node7 1.125  watchdog · fallback · light    │
  └─────────────────────────────────────────────┘
```

---

## Estado actual dos nós

| Node     | IP            | Hardware         | Roles                              | Estado        | SSH  |
|----------|---------------|------------------|------------------------------------|---------------|------|
| node1    | 192.168.1.111 | Mini PC          | control_plane (DB·UI·scheduler)    | ✅ ativo      | ✅  |
| nodecpu  | 192.168.1.172 | Servidor físico  | preprocess, general                | ✅ ativo      | ✅  |
| nodegpu  | 192.168.1.120 | Servidor RTX3090 | gpu_inference, llm_gpu, ai_analysis| ✅ ativo      | ✅  |
| node-nas | 192.168.1.118 | NAS              | nfs, backups, models               | ❌ offline    | ❌  |
| node2    | 192.168.1.112 | Mini PC          | ai_analysis                        | ✅ ativo      | ✅  |
| node4    | 192.168.1.122 | Mini PC          | radar, automation, ai_analysis     | ✅ ativo      | ✅  |
| node5    | 192.168.1.123 | Mini PC          | general (temporário)               | ✅ ativo      | ✅  |
| node6    | 192.168.1.124 | Mini PC          | general (temporário)               | ✅ ativo      | ✅  |
| node7    | 192.168.1.125 | Mini PC          | watchdog, fallback, light          | ✅ ativo      | ✅  |
| node3    | 192.168.1.121 | Mini PC          | —                                  | ❌ desligado  | ❌  |

---

## Execução de jobs — últimos 7 dias

| Node    | Total | Done | Failed | Taxa |
|---------|-------|------|--------|------|
| node4   | 20    | 20   | 0      | 100% |
| nodecpu | 6     | 6    | 0      | 100% |
| node5   | 2     | 2    | 0      | 100% |
| node6   | 2     | 2    | 0      | 100% |

*Nota: nodecpu entrou em produção a 2026-04-09 — volume baixo mas 100% sucesso.*

---

## Roles por nó (cluster_workers.json)

```json
node2    → ai_analysis
node4    → radar, automation, ai_analysis
node5    → general
node6    → general
node7    → watchdog, light, echo, fallback
nodecpu  → preprocess, general
nodegpu  → gpu_inference, llm_gpu  (registado, worker não instalado ainda)
```

---

## Serviços em nodecpu (systemd --user)

| Serviço                            | Tipo   | Estado  | Função                        |
|------------------------------------|--------|---------|-------------------------------|
| aios-cluster-worker.service        | daemon | ✅ run  | Worker preprocess+general     |
| aios-pipeline-scheduler.timer      | 60s    | ✅ run  | Cria jobs (incidents/radar/…) |
| aios-autonomia-orchestrator.timer  | 60s    | ✅ run  | Retry + zombie detection      |
| aios-watchdog.timer                | 60s    | ✅ run  | Watchdog stack                |

---

## Configuração nodecpu — particularidades

- **NFS não montado**: `/cluster/d1/ai-os/` é Docker volume (owned root)
- **Path remapping**: `cluster_worker.py` local substitui `/cluster/d1/ai-os/` → `/home/jdl/ai-os/` em runtime
- **EnvironmentFile**: `~/ai-os/config/aios.env`
  - `DATABASE_URL=postgresql+pg8000://aios_user:jdl@127.0.0.1:5432/aios`
  - `AIOS_ROOT=/home/jdl/ai-os`
  - `AIOS_CLUSTER_ROOT=/cluster/d1/ai-os`  ← scheduler usa este para criar payloads correctos para os NFS nodes

---

## Pendências

### Urgente
- [ ] **node-nas**: verificar estado físico / IP / conectividade

### Próximos passos
- [ ] Criar jobs `gpu_inference`/`llm_gpu` no pipeline_scheduler para aproveitar RTX 3090
- [ ] Configurar Ollama em nodegpu para usar GPU (verificar docker run --gpus all)
- [ ] Reduzir gradualmente jobs `general` em node5/node6
- [ ] Verificar se node5/node6 podem ser desligados sem impacto

### Descontinuado
- [x] node3 removido do cluster_workers.json e da tabela workers
- [x] Jobs failed limpos (3645 removidos em 2026-04-10)

---

## Fases de migração

| Fase | Objetivo                              | Estado      |
|------|---------------------------------------|-------------|
| 1    | Estabilizar arquitectura core         | ✅ Concluída |
| 2    | Consolidar general em nodecpu         | 🔄 Em curso |
| 3    | Separar CPU de GPU (nodegpu)          | ⏳ Pendente  |
| 4    | Saída progressiva node5/node6         | ⏳ Pendente  |
