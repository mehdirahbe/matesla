#!/usr/bin/env bash
# Install MaTesla as a systemd system service.
# Paths and User/Group are taken from the current clone + current user
# (templates: matesla-gunicorn.service.in).
#
# Starts at boot; Restart=no so a manual stop/kill stays down for runserver dev.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=config/_render_unit.sh
source "${SCRIPT_DIR}/_render_unit.sh"
matesla_resolve_paths

SERVICE_NAME="matesla-gunicorn.service"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}"

if [[ ! -x "${MATESLA_ROOT}/.venv/bin/gunicorn" ]]; then
  echo "gunicorn not found in ${MATESLA_ROOT}/.venv" >&2
  echo "Install dependencies first, e.g.: ./scripts/install-linux.sh" >&2
  echo "  or: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 1
fi

matesla_render_unit "${SCRIPT_DIR}/matesla-gunicorn.service.in" "$UNIT_PATH"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "Service installed and started: $SERVICE_NAME"
echo "  user / path : ${MATESLA_USER} @ ${MATESLA_ROOT}"
echo "  status      : systemctl status $SERVICE_NAME"
echo "  stop (before runserver) : sudo systemctl stop $SERVICE_NAME"
echo "  logs        : journalctl -u $SERVICE_NAME -f"
echo "  disable at boot : sudo systemctl disable $SERVICE_NAME"
echo "  uninstall   : ${SCRIPT_DIR}/uninstall_gunicorn_service.sh"
echo
echo "Open: http://127.0.0.1:8001"
