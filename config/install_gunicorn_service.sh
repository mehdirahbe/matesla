#!/bin/bash
# Install MaTesla as a systemd system service (same idea as PicturesDjango).
# Starts at boot; Restart=no so a manual kill stays down for runserver dev.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="matesla-gunicorn.service"

sudo cp "$SCRIPT_DIR/matesla-gunicorn.service" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Service installé et démarré : $SERVICE_NAME"
echo "  statut : systemctl status $SERVICE_NAME"
echo "  arrêt (avant runserver) : sudo systemctl stop $SERVICE_NAME"
echo "  logs : journalctl -u $SERVICE_NAME -f"
echo "  désactiver au boot : sudo systemctl disable $SERVICE_NAME"
echo
echo "Port : http://127.0.0.1:8001 (Tailscale Serve :8443 si déjà configuré)"
