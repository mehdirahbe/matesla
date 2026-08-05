#!/usr/bin/env bash
# Install (or refresh) the user crontab line that polls Tesla history.
# Hits the local capture endpoint every minute; adaptive spacing inside the app
# decides when Fleet is actually called.
#
# Usage:
#   ./scripts/install_capture_cron.sh
#   ./scripts/uninstall_capture_cron.sh
#
set -euo pipefail

CAPTURE_URL="${MATESLA_CAPTURE_URL:-http://127.0.0.1:8001/matesla/internal/capture}"
LOG_FILE="${MATESLA_CAPTURE_LOG:-/tmp/matesla-capture.log}"
# Marker so we can find/replace our line without touching other crontab entries
MARKER="matesla/internal/capture"

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required for capture cron (sudo apt install curl)" >&2
  exit 1
fi

# One line: timestamp + curl + blank line → append log
CRON_LINE="* * * * * { date -Iseconds; curl -fsS ${CAPTURE_URL}; echo; } >> ${LOG_FILE} 2>&1"

# Strip any previous MaTesla capture lines, then append the current one
existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$existing" | grep -vF "$MARKER" || true)"
# Avoid a leading blank line when crontab was empty
{
  if [[ -n "$filtered" ]]; then
    printf '%s\n' "$filtered"
  fi
  printf '%s\n' "$CRON_LINE"
} | crontab -

echo "Capture cron installed (every minute → ${CAPTURE_URL})"
echo "  log     : ${LOG_FILE}"
echo "  list    : crontab -l"
echo "  follow  : tail -f ${LOG_FILE}"
echo "  remove  : ./scripts/uninstall_capture_cron.sh"
echo
echo "Note: the web app must be running (systemd or runserver) for capture to work."
