#!/usr/bin/env bash
# Install (or refresh) the user crontab line that fetches Elia day-ahead prices.
# Runs 20:00 Europe/Brussels: D+1 is published around 13:00, so tonight and
# tomorrow are in cache before DayMap shows overnight home charging.
#
# Usage:
#   ./scripts/install_elia_cron.sh
#   ./scripts/uninstall_elia_cron.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${MATESLA_PYTHON:-$ROOT/.venv/bin/python}"
MANAGE="$ROOT/manage.py"
LOG_FILE="${MATESLA_ELIA_LOG:-/tmp/matesla-elia.log}"
MARKER="manage.py FetchEliaDayAhead"

if [[ ! -x "$PYTHON" ]]; then
  echo "Python venv missing: $PYTHON" >&2
  echo "Create it first (./scripts/install-linux.sh) or set MATESLA_PYTHON." >&2
  exit 1
fi
if [[ ! -f "$MANAGE" ]]; then
  echo "manage.py missing: $MANAGE" >&2
  exit 1
fi

# System TZ is Europe/Brussels on the ThinkPad; 20:00 is evening after the
# D+1 auction (~13:00). Lookback is inside the management command.
CRON_LINE="0 20 * * * { date -Iseconds; cd ${ROOT} && ${PYTHON} ${MANAGE} FetchEliaDayAhead; echo; } >> ${LOG_FILE} 2>&1"

existing="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$existing" | grep -vF "$MARKER" || true)"
{
  if [[ -n "$filtered" ]]; then
    printf '%s\n' "$filtered"
  fi
  printf '%s\n' "$CRON_LINE"
} | crontab -

echo "Elia cron installed (20:00 → FetchEliaDayAhead, lookback 7 days + tomorrow)"
echo "  log     : ${LOG_FILE}"
echo "  list    : crontab -l"
echo "  follow  : tail -f ${LOG_FILE}"
echo "  run now : cd ${ROOT} && ${PYTHON} ${MANAGE} FetchEliaDayAhead"
echo "  remove  : ./scripts/uninstall_elia_cron.sh"
echo
echo "The machine must be on at 20:00. Opening Charges still backfills a hole."
