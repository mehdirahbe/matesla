#!/usr/bin/env bash
# Expose MaTesla (local :8001) on Tailscale HTTPS (default :8443).
#
# Usage (as Tailscale operator, or with sudo):
#   ./scripts/tailscale-serve-matesla.sh
#
# Phone needs ACL to allow the destination HTTPS port (default 8443).
# See README — Access controls section.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND="${MATESLA_BACKEND:-http://127.0.0.1:8001}"
HTTPS_PORT="${MATESLA_TS_HTTPS_PORT:-8443}"

if ! command -v tailscale >/dev/null; then
  echo "tailscale CLI not found" >&2
  exit 1
fi

if ! curl -fsS -o /dev/null --max-time 2 "${BACKEND}/" 2>/dev/null \
  && ! curl -fsS -o /dev/null --max-time 2 "${BACKEND}/en/accounts/login/" 2>/dev/null; then
  echo "Backend not answering at ${BACKEND}"
  echo "Start MaTesla first, e.g.:"
  echo "  cd ${ROOT} && . .venv/bin/activate && python manage.py runserver 127.0.0.1:8001"
  echo "  or: systemctl start matesla-gunicorn.service"
  exit 1
fi

echo "Configuring MaTesla Serve: https://*:${HTTPS_PORT} → ${BACKEND}"
tailscale serve --bg --yes --https="${HTTPS_PORT}" "${BACKEND}"

echo
echo "Current serve config:"
tailscale serve status

DNS="$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
if [[ -n "${DNS}" ]]; then
  echo
  echo "MaTesla: https://${DNS}:${HTTPS_PORT}/"
  echo
fi
