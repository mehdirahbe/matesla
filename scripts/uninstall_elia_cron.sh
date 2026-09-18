#!/usr/bin/env bash
# Remove MaTesla Elia day-ahead line(s) from the current user's crontab.
set -euo pipefail

MARKER="manage.py FetchEliaDayAhead"
existing="$(crontab -l 2>/dev/null || true)"

if ! printf '%s\n' "$existing" | grep -qF "$MARKER"; then
  echo "No MaTesla Elia cron line found."
  exit 0
fi

filtered="$(printf '%s\n' "$existing" | grep -vF "$MARKER" || true)"
if [[ -z "$filtered" ]]; then
  crontab -r 2>/dev/null || true
else
  printf '%s\n' "$filtered" | crontab -
fi

echo "Elia cron removed."
echo "  remaining: crontab -l"
