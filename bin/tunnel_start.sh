#!/bin/bash
# Inicia tunnel SSH via localhost.run e actualiza AIOS_UI_BASE em .env.db

ENV_FILE="$HOME/.env.db"
LOG="/tmp/aios-tunnel.log"

# Matar tunnel anterior
pkill -f "ssh.*localhost.run" 2>/dev/null
sleep 1

# Iniciar tunnel e capturar URL
ssh -o StrictHostKeyChecking=no \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -R 80:localhost:3000 nokey@localhost.run 2>&1 | tee "$LOG" | while IFS= read -r line; do
    echo "$line"
    if [[ "$line" == *"lhr.life"* ]]; then
        URL=$(echo "$line" | grep -oP 'https://[a-z0-9]+\.lhr\.life')
        if [[ -n "$URL" ]]; then
            echo ">>> Tunnel URL: $URL"
            # Actualizar AIOS_UI_BASE em .env.db
            if grep -q "AIOS_UI_BASE" "$ENV_FILE"; then
                sed -i "s|AIOS_UI_BASE=.*|AIOS_UI_BASE=$URL|" "$ENV_FILE"
            else
                echo "AIOS_UI_BASE=$URL" >> "$ENV_FILE"
            fi
            # Reiniciar telegram bot para apanhar novo URL
            sudo systemctl restart aios-telegram.service 2>/dev/null
            echo ">>> AIOS_UI_BASE actualizado: $URL"
        fi
    fi
done
