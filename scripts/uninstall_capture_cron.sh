#!/usr/bin/env bash
# Remove MaTesla capture line(s) from the current user's crontab.
set -euo pipefail

MARKER="matesla/internal/capture"
existing="$(crontab -l 2>/dev/null || true)"

if ! printf '%s\n' "$existing" | grep -qF "$MARKER"; then
  echo "No MaTesla capture cron line found."
  exit 0
fi

filtered="$(printf '%s\n' "$existing" | grep -vF "$MARKER" || true)"
if [[ -z "$filtered" ]]; then
  # Empty crontab: remove it entirely if supported
  crontab -r 2>/dev/null || true
else
  printf '%s\n' "$filtered" | crontab -
fi

echo "Capture cron removed."
echo "  remaining: crontab -l"
