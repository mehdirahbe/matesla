#!/usr/bin/env bash
# Shared helpers for installing systemd units from *.service.in templates.
# Sourced by install_*.sh — not meant to be run alone.
# shellcheck shell=bash

matesla_resolve_paths() {
  # SCRIPT_DIR must be set by the caller (directory of the install script = config/).
  MATESLA_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
  if [[ "$(id -u)" -eq 0 ]]; then
    echo "Do not run this script as root. Run it as a normal user (sudo will be prompted)." >&2
    exit 1
  fi
  MATESLA_USER="${MATESLA_USER:-$USER}"
  MATESLA_GROUP="${MATESLA_GROUP:-$(id -gn)}"
}

matesla_render_unit() {
  # Usage: matesla_render_unit template.in /path/to/dest.service
  local template="$1"
  local dest="$2"
  if [[ ! -f "$template" ]]; then
    echo "Missing template: $template" >&2
    exit 1
  fi
  sed \
    -e "s|@MATESLA_ROOT@|${MATESLA_ROOT}|g" \
    -e "s|@MATESLA_USER@|${MATESLA_USER}|g" \
    -e "s|@MATESLA_GROUP@|${MATESLA_GROUP}|g" \
    "$template" | sudo tee "$dest" >/dev/null
}
