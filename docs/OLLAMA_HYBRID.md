# Ollama Hybrid Architecture

## Overview

```
ASUS/WSL2 (controller)
  ├─ Docker Ollama  → localhost:11434  — TIER 1 asus_gpu  (RTX 4050, 28 tok/s)
  └─ SSH tunnel     → localhost:11435  → node1:11434
                                         TIER 2 cluster_cpu

Cluster nodes (via LAN)
  ├─ node1  192.168.1.111  24GB  Ollama snap → bound to 127.0.0.1 (via tunnel)
  ├─ node2  192.168.1.112  16GB  Ollama NFS  → 0.0.0.0:11434  (~0.5 tok/s)
  ├─ node4  192.168.1.122  12GB  Ollama NFS  → 0.0.0.0:11434
  └─ node3  192.168.1.121   8GB  not deployed (RAM tight)
```

## Providers

| Name       | URL                         | Tier | Model              | Speed  |
|------------|-----------------------------|------|--------------------|--------|
| asus_gpu   | http://localhost:11434      | 1    | qwen2.5-coder:7b   | ~30 tok/s |
| node1_cpu  | http://localhost:11435      | 2    | qwen2.5-coder:7b   | ~5 tok/s  |
| node2_cpu  | http://192.168.1.112:11434  | 2    | qwen2.5-coder:7b   | ~0.5 tok/s |
| node4_cpu  | http://192.168.1.122:11434  | 2    | qwen2.5-coder:7b   | ~0.5 tok/s |

## Routing Rules (model_router.py)

| Task type / keyword        | Provider   |
|----------------------------|------------|
| DEV_TASK, ARCH_TASK, REFACTOR, high_priority≥8, "implement", "architecture"... | claude |
| CODING, CODE_REVIEW, DEBUG, "code", "fix bug", "debug"... | asus_gpu |
| OPS, MONITORING, CLASSIFY, ROUTINE, "status", "alert", "check"... | cluster_cpu |
| default                    | cluster_cpu |
| Fallback: tier down → next tier → claude |

## NFS Shared Models

Models live on NFS — downloaded once, shared across all cluster nodes:

```
/cluster/d1/ollama/
  bin/ollama          ← Ollama binary (x86_64)
  models/
    blobs/            ← model weights (7b=4.4GB, 14b=8.4GB)
    manifests/        ← model metadata
```

## Daily Commands

```bash
# Health check
~/ai-os/bin/check_ollama_hybrid.sh

# Inference test
~/ai-os/bin/test_ollama_hybrid.sh

# Router status
cd ~/ai-os && python3 bin/model_router.py status

# Force a specific provider
python3 bin/model_router.py set_override asus_gpu   # or cluster_cpu, claude, ""

# Query routing decision
python3 bin/model_router.py '{"task_type":"CODING","goal":"fix bug"}'

# Aider — ASUS GPU (preferred)
cd ~/ai-os
OLLAMA_API_BASE=http://localhost:11434 aider --model ollama/qwen2.5-coder:7b --no-auto-commits

# Aider — node1 via tunnel (fallback)
OLLAMA_API_BASE=http://localhost:11435 aider --model ollama/qwen2.5-coder:7b --no-auto-commits
```

## Cluster Node Management

```bash
# Start Ollama on node2 or node4
ssh jdl@192.168.1.112 "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user start aios-ollama.service"

# Check status on node
ssh jdl@192.168.1.112 "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user status aios-ollama.service"

# Stop Ollama on node
ssh jdl@192.168.1.112 "XDG_RUNTIME_DIR=/run/user/$(id -u) systemctl --user stop aios-ollama.service"

# Restart all cluster Ollama nodes
for ip in 192.168.1.112 192.168.1.122; do
  ssh jdl@$ip "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user restart aios-ollama.service"
done
```

## SSH Tunnel (node1)

```bash
# Status
systemctl --user status ollama-node1-tunnel.service

# Restart
systemctl --user restart ollama-node1-tunnel.service

# Manual tunnel (if service down)
ssh -f -N -L 11435:localhost:11434 jdl@192.168.1.111
```

## Rollback

```bash
# Stop cluster nodes
for ip in 192.168.1.112 192.168.1.122; do
  ssh jdl@$ip "XDG_RUNTIME_DIR=/run/user/\$(id -u) systemctl --user stop aios-ollama.service"
done

# Stop tunnel
systemctl --user stop ollama-node1-tunnel.service

# Force ASUS only
cd ~/ai-os && python3 bin/model_router.py set_override asus_gpu

# Revert to all-Claude
export HYBRID_MODE=false
```

## Troubleshooting

**node down after reboot**: `loginctl enable-linger jdl` must be set — already done.

**NFS models not loading**: check NFS mount with `ls /cluster/d1/ollama/models/`

**ASUS Docker Ollama not using GPU**: `docker exec ollama nvidia-smi` should show GPU.
If blank: `sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker && cd ~/ai-os && docker rm -f ollama && docker compose up -d ollama`

**node2/4 very slow (0.5 tok/s)**: Normal for i3-class CPU with 7b model. Use for batch/async only. Interactive → asus_gpu or node1.
