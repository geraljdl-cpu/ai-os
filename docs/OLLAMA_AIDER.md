# Ollama + Aider — Inferência Local

## Arquitectura

```
ASUS (WSL2 192.168.1.101)
  ├─ Docker Ollama  → localhost:11434  (qwen2.5-coder:14b, nomic-embed-text)
  └─ SSH tunnel     → localhost:11435  → node1:11434 (qwen2.5-coder:14b)

node1 (192.168.1.210, i5/24GB)
  └─ Snap Ollama    → 127.0.0.1:11434 (qwen2.5-coder:14b, qwen2.5-coder:7b)
```

## Endpoints

| Endpoint | Via | Modelo disponível |
|---|---|---|
| `http://localhost:11434` | Docker local (ASUS) | qwen2.5-coder:14b, nomic-embed-text |
| `http://localhost:11435` | SSH tunnel → node1 | qwen2.5-coder:14b (após pull) |

## Lançar Aider

### Contra Docker local (ASUS)
```bash
cd ~/ai-os
OLLAMA_API_BASE=http://localhost:11434 aider \
  --model ollama/qwen2.5-coder:14b \
  --no-auto-commits
```

### Contra node1 (tunnel activo)
```bash
# 1. Abrir tunnel (se não estiver activo)
ssh -f -N -L 11435:localhost:11434 jdl@192.168.1.210

# 2. Lançar Aider
cd ~/ai-os
OLLAMA_API_BASE=http://localhost:11435 aider \
  --model ollama/qwen2.5-coder:14b \
  --no-auto-commits
```

## SSH Tunnel Persistente (systemd user)

Para o tunnel sobreviver a reboots do ASUS/WSL2:

```bash
# Criar serviço
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/ollama-node1-tunnel.service << 'EOF'
[Unit]
Description=SSH tunnel para Ollama node1
After=network.target

[Service]
ExecStart=/usr/bin/ssh -N -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -L 11435:localhost:11434 jdl@192.168.1.210
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now ollama-node1-tunnel.service
```

## .aiderignore

Ficheiro `~/ai-os/.aiderignore` exclui:
- `data/`, `pylib/`, `node_modules/` — pesado, sem relevância para edição
- `runtime/` — estado volátil
- `*.log`, `*.sqlite`, `*.db` — ficheiros binários/voláteis
- `.aider.chat.history.md`, `.aider.input.history` — histórico Aider

## Modelos no node1

```bash
# Ver modelos instalados
ssh jdl@192.168.1.210 "ollama list"

# Pull de modelo novo
ssh jdl@192.168.1.210 "ollama pull <modelo>"

# Estado do serviço snap
ssh jdl@192.168.1.210 "snap services ollama"
```

## Rollback

```bash
# Parar tunnel node1
systemctl --user stop ollama-node1-tunnel.service

# Voltar a usar Docker Ollama local
export OLLAMA_API_BASE=http://localhost:11434
# (nenhuma outra alteração necessária — Docker Ollama continua activo)
```
