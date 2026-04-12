# Cluster Inventory — AI-OS
*Actualizado: 2026-04-12 (migração netplan concluída — IPs físicos definitivos)*

## Topologia

```
                    ┌─────────────┐
                    │    node1    │  192.168.1.210
                    │ control-    │  scheduler · DB · UI
                    │  plane      │  agent-router · redis
                    └──────┬──────┘
                           │ LAN 192.168.1.0/24
        ┌──────────────────┼──────────────────┐
        │                  │                  │
  ┌─────┴──────┐    ┌──────┴──────┐    ┌──────┴──────┐
  │  nodecpu   │    │   nodegpu   │    │  node-nas   │
  │ cpu-main   │    │  gpu-main   │    │  storage    │
  │  .201      │    │   .202      │    │   .203      │
  │ preprocess │    │ gpu_infer.  │    │  NFS · bkp  │
  │ general    │    │ llm_gpu     │    │  models     │
  └────────────┘    └────────────┘    └─────────────┘
        │
  ┌─────┴──── Auxiliares ────────────┐
  │ node2  .211  ai_analysis         │
  │ node3  .212  general · fallback  │
  └──────────────────────────────────┘
```

---

## Estado actual dos nós

| Node     | IP            | Hardware         | Roles                               | Estado        | SSH  |
|----------|---------------|------------------|-------------------------------------|---------------|------|
| node1    | 192.168.1.210 | Mini PC          | control_plane (DB·UI·scheduler)     | ✅ ativo      | ✅  |
| nodecpu  | 192.168.1.201 | Servidor físico  | preprocess, general                 | ✅ ativo      | ✅  |
| nodegpu  | 192.168.1.202 | Servidor RTX3090 | gpu_inference, llm_gpu, ai_analysis | ✅ ativo      | ✅  |
| node-nas | 192.168.1.203 | Mini PC          | nfs, backups, models                | ✅ ativo      | ✅  |
| node2    | 192.168.1.211 | Mini PC          | ai_analysis                         | ✅ ativo      | ✅  |
| node3    | 192.168.1.212 | Mini PC          | general, fallback                   | ❌ desligado  | ❌  |

*Nodes 4–7 removidos do cluster.*

---

## Execução de jobs — últimos 7 dias

| Node    | Total | Done | Failed | Taxa |
|---------|-------|------|--------|------|
| nodecpu | 6     | 6    | 0      | 100% |
| node2   | —     | —    | —      | —    |

---

## Roles por nó (cluster_workers.json)

```json
nodecpu  → preprocess, general
nodegpu  → gpu_inference, llm_gpu, ai_analysis
node1    → coordinator
node2    → ai_analysis
node3    → general, fallback
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
  - `AIOS_CLUSTER_ROOT=/cluster/d1/ai-os`

---

## NFS — node-nas

- **Servidor**: 192.168.1.203 (node-nas)
- **Export**: `/cluster-storage` → montado em `/cluster` em todos os nodes
- **Alias temporário**: 192.168.1.126 (mantido para compatibilidade durante transição)
- **fstab actualizado**: todos os nodes apontam para 192.168.1.203

---

## Pendências

### Próximos passos
- [ ] Activar node3 (.212) como worker de fallback
- [ ] Remover alias 192.168.1.126 de node-nas após confirmar todos os nodes montam de .203

### Descontinuado
- [x] nodes 4/5/6/7 removidos do cluster_workers.json
- [x] Jobs failed limpos (3645 removidos em 2026-04-10)
- [x] GPU pipeline activo em nodegpu (radar_gpu → llm_gpu)
- [x] IPs físicos migrados via netplan (2026-04-12): node1→.210, nodecpu→.201, nodegpu→.202, node-nas→.203, node2→.211
- [x] node-nas identificado (era node8 em .126), renomeado e migrado para .203

---

## Fases de migração

| Fase | Objetivo                              | Estado       |
|------|---------------------------------------|--------------|
| 1    | Estabilizar arquitectura core         | ✅ Concluída |
| 2    | Consolidar general em nodecpu         | ✅ Concluída |
| 3    | Separar CPU de GPU (nodegpu)          | ✅ Concluída |
| 4    | Normalização IPs (201-212)            | ✅ Concluída |
| 5    | Saída definitiva node5/node6          | ✅ Concluída |
