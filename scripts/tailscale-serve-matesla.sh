#!/usr/bin/env bash
# Expose MaTesla (runserver :8001) on Tailscale HTTPS :8443.
# Does NOT touch :443 — that stays for PicturesDjango (:8000).
#
# Usage (as Tailscale operator, or with sudo):
#   ./scripts/tailscale-serve-matesla.sh
#
# Phone needs ACL to allow destination port 8443 (this tailnet currently
# only allows :443 — open Access controls if the phone cannot connect).
#
#   https://mehdi-thinkbook-13s-g2-itl.taila97662.ts.net:8443/fr/

set -euo pipefail

BACKEND="${MATESLA_BACKEND:-http://127.0.0.1:8001}"
HTTPS_PORT="${MATESLA_TS_HTTPS_PORT:-8443}"
# Photos app — never overwrite unless explicitly requested
PHOTOS_BACKEND="${PHOTOS_BACKEND:-http://127.0.0.1:8000}"
RESTORE_PHOTOS_443="${RESTORE_PHOTOS_443:-1}"

if ! command -v tailscale >/dev/null; then
  echo "tailscale CLI not found" >&2
  exit 1
fi

if ! curl -fsS -o /dev/null --max-time 2 "${BACKEND}/fr/accounts/login/" 2>/dev/null; then
  echo "Backend not answering at ${BACKEND}"
  echo "Start Django first, e.g.:"
  echo "  cd /home/mehdi/PycharmProjects/matesla && . .venv/bin/activate && python manage.py runserver 127.0.0.1:8001"
  exit 1
fi

if [[ "${RESTORE_PHOTOS_443}" == "1" ]]; then
  echo "Ensuring photos on https://*:443 → ${PHOTOS_BACKEND}"
  tailscale serve --bg --yes --https=443 "${PHOTOS_BACKEND}"
fi

echo "Configuring MaTesla Serve: https://*:${HTTPS_PORT} → ${BACKEND}"
tailscale serve --bg --yes --https="${HTTPS_PORT}" "${BACKEND}"

echo
echo "Current serve config:"
tailscale serve status

DNS="$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json; print(json.load(sys.stdin).get("Self",{}).get("DNSName","").rstrip("."))' 2>/dev/null || true)"
if [[ -n "${DNS}" ]]; then
  echo
  echo "Photos (443):  https://${DNS}/"
  echo "MaTesla (8443): https://${DNS}:${HTTPS_PORT}/fr/"
  echo
  echo "If the phone cannot open :8443, allow port 8443 in Tailscale ACL"
  echo "(admin → Access controls). Local-only access works without that."
fi
