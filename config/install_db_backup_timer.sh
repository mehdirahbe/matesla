#!/bin/bash
# Weekly full-DB backup timer (4 archives in db-backups/, zstd -3).
# Does NOT upload to Dropbox — copy db-backups/ yourself to USB / cloud.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="matesla-db-backup.service"
TIMER_NAME="matesla-db-backup.timer"

sudo cp "$SCRIPT_DIR/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
sudo cp "$SCRIPT_DIR/$TIMER_NAME" "/etc/systemd/system/$TIMER_NAME"
sudo systemctl daemon-reload
sudo systemctl enable --now "$TIMER_NAME"

echo "Timer installé : $TIMER_NAME"
echo "  prochaines runs : systemctl list-timers $TIMER_NAME"
echo "  lancer maintenant : sudo systemctl start $SERVICE_NAME"
echo "  logs : journalctl -u $SERVICE_NAME -n 50"
echo "  archives : $(cd "$SCRIPT_DIR/.." && pwd)/db-backups/"
echo
echo "Dropbox / USB : copie manuelle de db-backups/matesla-db-*.sqlite3.zst"
