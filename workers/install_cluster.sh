#!/bin/bash
# install_cluster.sh — Deploy aios-cluster-worker em cada node (sem root)
# Correr em cada node: bash /cluster/d1/ai-os/workers/install_cluster.sh

set -e

NODE_NAME=$(hostname)
AIOS_ROOT="/cluster/d1/ai-os"
SYSTEMD_DIR="$HOME/.config/systemd/user"
SERVICE_SRC="${AIOS_ROOT}/workers/aios-cluster-worker.service"

echo ">>> AI-OS Cluster Worker — instalação em $NODE_NAME"

# ── Verificar deps ────────────────────────────────────────────────────────────
echo "Verificando deps Python (pylib NFS)..."
python3 -c "
import sys; sys.path.insert(0, '/cluster/d1/ai-os/pylib')
import sqlalchemy, pg8000
print(f'  sqlalchemy {sqlalchemy.__version__}, pg8000 {pg8000.__version__} — OK')
"

# ── Linger (serviço arranca sem sessão activa) ─────────────────────────────────
loginctl enable-linger "$(whoami)" 2>/dev/null || true

# ── Criar log dir ─────────────────────────────────────────────────────────────
mkdir -p "${AIOS_ROOT}/runtime/workers/${NODE_NAME}"

# ── Instalar systemd user service ─────────────────────────────────────────────
mkdir -p "$SYSTEMD_DIR"
cp "$SERVICE_SRC" "$SYSTEMD_DIR/aios-cluster-worker.service"

export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user daemon-reload
systemctl --user enable aios-cluster-worker.service
systemctl --user restart aios-cluster-worker.service

# ── Status ────────────────────────────────────────────────────────────────────
sleep 2
echo ""
systemctl --user status aios-cluster-worker.service --no-pager | head -15
echo ""
echo "Logs: journalctl --user -u aios-cluster-worker.service -f"
echo "Done: $NODE_NAME"
