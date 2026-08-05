#!/usr/bin/env bash
# Bootstrap MaTesla on Linux (Ubuntu/Debian-like): venv, deps, migrate,
# collectstatic, systemd service, capture cron, and menu shortcut.
#
# Usage (from anywhere):
#   ./scripts/install-linux.sh
#   ./scripts/install-linux.sh --no-service      # venv + DB only
#   ./scripts/install-linux.sh --no-cron         # skip history capture crontab
#   ./scripts/install-linux.sh --with-backup     # also weekly DB backup timer
#   ./scripts/install-linux.sh --no-desktop      # skip .desktop launcher
#
# Needs: python3, python3-venv, python3-pip, curl (sudo only for systemd).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

INSTALL_SERVICE=1
INSTALL_CRON=1
INSTALL_BACKUP=0
INSTALL_DESKTOP=1

usage() {
  sed -n '2,/^[^#]/p' "$0" | sed -e '/^[^#]/d' -e 's/^# \?//'
  exit 0
}

for arg in "$@"; do
  case "$arg" in
    --no-service)  INSTALL_SERVICE=0 ;;
    --no-cron)     INSTALL_CRON=0 ;;
    --with-backup) INSTALL_BACKUP=1 ;;
    --no-desktop)  INSTALL_DESKTOP=0 ;;
    -h|--help)     usage ;;
    *)
      echo "Unknown option: $arg (see --help)" >&2
      exit 1
      ;;
  esac
done

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Do not run this script as root. Run it as a normal user." >&2
  exit 1
fi

echo "==> MaTesla — Linux install"
echo "    directory: $ROOT"
echo

# --- system packages hint ---------------------------------------------------
need_pkg=()
command -v python3 >/dev/null 2>&1 || need_pkg+=(python3)
python3 -c "import venv" 2>/dev/null || need_pkg+=(python3-venv)
# ensure pip bootstrap possible
python3 -c "import ensurepip" 2>/dev/null || need_pkg+=(python3-pip)

if ((${#need_pkg[@]})); then
  echo "Missing packages: ${need_pkg[*]}"
  echo "Install them, then re-run:"
  echo "  sudo apt update && sudo apt install -y ${need_pkg[*]}"
  exit 1
fi

if [[ "$INSTALL_BACKUP" -eq 1 ]] && ! command -v zstd >/dev/null 2>&1; then
  echo "zstd is required for --with-backup:"
  echo "  sudo apt install -y zstd"
  exit 1
fi

# --- venv + requirements ----------------------------------------------------
if [[ ! -d .venv ]]; then
  echo "==> Creating venv (.venv)"
  python3 -m venv .venv
else
  echo "==> Venv already present (.venv)"
fi

# shellcheck disable=SC1091
source .venv/bin/activate
echo "==> Installing Python dependencies"
python -m pip install -U pip
python -m pip install -r requirements.txt

# --- Django bootstrap -------------------------------------------------------
echo "==> Database migrations (SQLite by default)"
python manage.py migrate --noinput

echo "==> Static files (collectstatic)"
python manage.py collectstatic --noinput

if [[ ! -f .env ]]; then
  echo
  echo "Note: no .env file — Tesla Fleet credentials can be configured later"
  echo "(.env file or in-app setup). See mysite/settings.py"
  echo "(TESLA_CLIENT_ID / TESLA_CLIENT_SECRET, …)."
fi

# --- systemd web service ----------------------------------------------------
if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  echo
  echo "==> systemd service (gunicorn on 127.0.0.1:8001)"
  "$ROOT/config/install_gunicorn_service.sh"
else
  echo
  echo "==> systemd service skipped (--no-service)"
  echo "    To start manually:"
  echo "      source .venv/bin/activate"
  echo "      python manage.py runserver 127.0.0.1:8001"
fi

# --- capture cron (history collection) --------------------------------------
if [[ "$INSTALL_CRON" -eq 1 ]]; then
  echo
  echo "==> Capture cron (every minute → local history endpoint)"
  if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required for capture cron:"
    echo "  sudo apt install -y curl"
    echo "Then re-run: ./scripts/install_capture_cron.sh"
  else
    "$ROOT/scripts/install_capture_cron.sh"
  fi
else
  echo
  echo "==> Capture cron skipped (--no-cron)"
  echo "    Without it, graphs/history stay empty after install."
  echo "    Add later: ./scripts/install_capture_cron.sh"
fi

# --- optional weekly backup timer -------------------------------------------
if [[ "$INSTALL_BACKUP" -eq 1 ]]; then
  echo
  echo "==> Weekly backup timer"
  "$ROOT/config/install_db_backup_timer.sh"
fi

# --- desktop / menu launcher ------------------------------------------------
if [[ "$INSTALL_DESKTOP" -eq 1 ]]; then
  APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
  mkdir -p "$APP_DIR"
  DESKTOP_FILE="${APP_DIR}/matesla.desktop"
  cat >"$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=MaTesla
Comment=Local Tesla tracking (http://127.0.0.1:8001)
Exec=xdg-open http://127.0.0.1:8001
Icon=applications-internet
Terminal=false
Categories=Network;Utility;
StartupNotify=true
EOF
  chmod 644 "$DESKTOP_FILE"
  # Best-effort refresh of desktop database (ignore errors on minimal installs)
  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_DIR" 2>/dev/null || true
  fi
  echo
  echo "==> Menu launcher: $DESKTOP_FILE"
  echo "    (opens the browser on http://127.0.0.1:8001 — service must be running)"

  DESKTOP_DIR="${XDG_DESKTOP_DIR:-$HOME/Desktop}"
  if [[ -d "$DESKTOP_DIR" ]]; then
    cp "$DESKTOP_FILE" "$DESKTOP_DIR/matesla.desktop"
    chmod +x "$DESKTOP_DIR/matesla.desktop" 2>/dev/null || true
    echo "    also copied to desktop: $DESKTOP_DIR/matesla.desktop"
  fi
fi

echo
echo "========================================"
echo "  Install complete."
echo "  Open: http://127.0.0.1:8001"
echo "========================================"
echo
echo "Useful commands:"
echo "  systemctl status matesla-gunicorn.service"
echo "  sudo systemctl stop matesla-gunicorn.service    # before a dev runserver"
echo "  sudo systemctl start matesla-gunicorn.service"
echo "  journalctl -u matesla-gunicorn.service -f"
echo "  crontab -l                                      # capture cron"
echo "  tail -f /tmp/matesla-capture.log"
echo "  ./scripts/uninstall_capture_cron.sh"
echo "  ./config/uninstall_gunicorn_service.sh"
echo
if [[ "$INSTALL_SERVICE" -eq 1 ]]; then
  if systemctl is-active --quiet matesla-gunicorn.service 2>/dev/null; then
    echo "Service is active."
  else
    echo "Service does not appear active — check: systemctl status matesla-gunicorn.service" >&2
  fi
fi
if [[ "$INSTALL_CRON" -eq 1 ]] && crontab -l 2>/dev/null | grep -qF 'matesla/internal/capture'; then
  echo "Capture cron is installed."
fi
