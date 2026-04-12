#!/bin/bash
# install_node.sh — Deploy aios-cluster-worker on this node (no root needed)
# Uses systemd --user so no sudo required.
# Usage: bash /cluster/ai-os/workers/install_node.sh

set -e

NODE_NAME=$(hostname)
WORKERS_DIR="/cluster/ai-os/workers"
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"

# ── Node role assignment ──────────────────────────────────────────────────────
case "$NODE_NAME" in
  node1) ROLE="coordinator" ;;
  node2) ROLE="ai_analysis" ;;
  node3) ROLE="general" ;;
  nodecpu) ROLE="preprocess" ;;
  nodegpu) ROLE="gpu_inference" ;;
  *)     ROLE="general" ;;
esac

echo ">>> Installing aios-cluster-worker on $NODE_NAME (role: $ROLE)"

# ── Python deps (pre-installed in /cluster/ai-os/pylib, no pip needed) ───────
echo "Verifying Python deps from NFS pylib..."
python3 -c "
import sys; sys.path.insert(0, '/cluster/ai-os/pylib')
import sqlalchemy, pg8000
print('sqlalchemy', sqlalchemy.__version__, 'pg8000', pg8000.__version__)
"
echo "Done."

# ── Systemd user service ──────────────────────────────────────────────────────
mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/aios-cluster-worker.service" << SERVICE_EOF
[Unit]
Description=AI-OS Cluster Worker ($NODE_NAME)
After=network.target

[Service]
Type=simple
WorkingDirectory=/cluster/ai-os
Environment=AIOS_DB_HOST=192.168.1.201
Environment=AIOS_DB_PORT=5432
Environment=AIOS_DB_USER=aios_user
Environment=AIOS_DB_PASS=jdl
Environment=AIOS_DB_NAME=aios
Environment=AIOS_OLLAMA=http://192.168.1.202:11434
Environment=AIOS_POLL_SEC=5
Environment=AIOS_NODE_NAME=$NODE_NAME
Environment=AIOS_NODE_ROLE=$ROLE
ExecStart=/usr/bin/python3 ${WORKERS_DIR}/cluster_worker.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
SERVICE_EOF

# ── Enable linger so user services survive logout ─────────────────────────────
if command -v loginctl &>/dev/null; then
  loginctl enable-linger "$(whoami)" 2>/dev/null || true
fi

# ── Start service ─────────────────────────────────────────────────────────────
export XDG_RUNTIME_DIR="/run/user/$(id -u)"
systemctl --user daemon-reload
systemctl --user enable aios-cluster-worker.service
systemctl --user restart aios-cluster-worker.service

sleep 2
echo ""
echo "Service status:"
systemctl --user status aios-cluster-worker.service --no-pager | head -15
echo ""
echo "Done! $NODE_NAME running as role=$ROLE"
echo "Logs: journalctl --user -u aios-cluster-worker.service -f"
