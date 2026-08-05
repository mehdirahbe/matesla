#!/usr/bin/env bash
# Weekly full-DB backup timer (4 archives in db-backups/, zstd -3).
# Does NOT upload backups — copy db-backups/ yourself to USB / cloud.
# Paths/User come from the current clone + current user (*.service.in).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=config/_render_unit.sh
source "${SCRIPT_DIR}/_render_unit.sh"
matesla_resolve_paths

SERVICE_NAME="matesla-db-backup.service"
TIMER_NAME="matesla-db-backup.timer"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"
TIMER_PATH="/etc/systemd/system/${TIMER_NAME}"

if [[ ! -x "${MATESLA_ROOT}/scripts/backup_db.sh" ]]; then
  echo "Backup script missing: ${MATESLA_ROOT}/scripts/backup_db.sh" >&2
  exit 1
fi
if ! command -v zstd >/dev/null 2>&1; then
  echo "zstd is required for backups (sudo apt install zstd)" >&2
  exit 1
fi

matesla_render_unit "${SCRIPT_DIR}/matesla-db-backup.service.in" "$SERVICE_PATH"
sudo cp "${SCRIPT_DIR}/${TIMER_NAME}" "$TIMER_PATH"
sudo systemctl daemon-reload
sudo systemctl enable --now "$TIMER_NAME"

echo "Timer installed: $TIMER_NAME"
echo "  user / path : ${MATESLA_USER} @ ${MATESLA_ROOT}"
echo "  next runs   : systemctl list-timers $TIMER_NAME"
echo "  run now     : sudo systemctl start $SERVICE_NAME"
echo "  logs        : journalctl -u $SERVICE_NAME -n 50"
echo "  archives    : ${MATESLA_ROOT}/db-backups/"
echo
echo "USB / cloud: copy db-backups/matesla-db-*.sqlite3.zst yourself"
