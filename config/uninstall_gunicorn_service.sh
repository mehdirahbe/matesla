#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="matesla-gunicorn.service"

sudo systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
echo "Service uninstalled: $SERVICE_NAME"
