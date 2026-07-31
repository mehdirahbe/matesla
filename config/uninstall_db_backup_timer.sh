#!/bin/bash
set -euo pipefail

SERVICE_NAME="matesla-db-backup.service"
TIMER_NAME="matesla-db-backup.timer"

sudo systemctl disable --now "$TIMER_NAME" 2>/dev/null || true
sudo systemctl disable --now "$SERVICE_NAME" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/$SERVICE_NAME" "/etc/systemd/system/$TIMER_NAME"
sudo systemctl daemon-reload
echo "Timer / service désinstallés : $TIMER_NAME"
echo "(Les fichiers dans db-backups/ sont laissés intacts.)"
