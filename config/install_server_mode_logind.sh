#!/bin/bash
# AC power: a closed lid or a locked session must not suspend the machine.
# Same installer in matesla and PicturesDjango. It writes three system files.
# A second run, from either repo, leaves those files alone when the settings
# are already in place: no logind restart, no GDM dconf restart.
#
# Since Ubuntu 26.04, GDM applies its own power settings once the session
# locks. The packaged profile does not load system-db:gdm, so a keyfile in
# /etc/dconf/db/gdm.d is ignored until /etc/dconf/profile/gdm lists that db.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOGIND_DROPIN=/etc/systemd/logind.conf.d/server-mode.conf
GDM_KEYFILE=/etc/dconf/db/gdm.d/90-no-suspend-ac
GDM_PROFILE=/etc/dconf/profile/gdm
STOCK_GDM_PROFILE=/usr/share/dconf/profile/gdm

logind_ready() {
  [[ -f "$LOGIND_DROPIN" ]] || return 1
  awk '
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    /^[[:space:]]*HandleLidSwitchExternalPower=/ { ext = $0 }
    /^[[:space:]]*HandleLidSwitch=/ { lid = $0 }
    END {
      ext_ok = (ext ~ /=ignore[[:space:]]*$/)
      lid_ok = (lid ~ /=suspend[[:space:]]*$/)
      exit (ext_ok && lid_ok) ? 0 : 1
    }
  ' "$LOGIND_DROPIN"
}

gdm_keys_ready() {
  [[ -f "$GDM_KEYFILE" ]] || return 1
  awk '
    function unquote(v) {
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", v)
      gsub(/^['\''"]|['\''"]$/, "", v)
      return v
    }
    /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
    /^[[:space:]]*lid-close-ac-action=/ {
      lid = unquote(substr($0, index($0, "=") + 1))
    }
    /^[[:space:]]*sleep-inactive-ac-type=/ {
      idle = unquote(substr($0, index($0, "=") + 1))
    }
    END { exit (lid == "nothing" && idle == "nothing") ? 0 : 1 }
  ' "$GDM_KEYFILE"
}

# $1 is a profile file. Ready when it loads system-db:gdm and still contains
# every directive from the packaged profile (so an Ubuntu profile change is
# not treated as already done).
profile_covers_stock() {
  local have=$1
  [[ -f "$have" && -f "$STOCK_GDM_PROFILE" ]] || return 1
  awk '
    function norm(s) {
      sub(/^[[:space:]]+/, "", s)
      sub(/[[:space:]]+$/, "", s)
      return s
    }
    NR == FNR {
      if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next
      need[norm($0)] = 1
      next
    }
    {
      if ($0 ~ /^[[:space:]]*#/ || $0 ~ /^[[:space:]]*$/) next
      seen[norm($0)] = 1
    }
    END {
      if (!seen["system-db:gdm"]) exit 1
      for (k in need) if (!seen[k]) exit 1
    }
  ' "$STOCK_GDM_PROFILE" "$have"
}

profile_ready() {
  if [[ -f "$GDM_PROFILE" ]]; then
    profile_covers_stock "$GDM_PROFILE"
    return
  fi
  [[ -f "$STOCK_GDM_PROFILE" ]] || return 1
  grep -E -q '^[[:space:]]*system-db:gdm[[:space:]]*$' "$STOCK_GDM_PROFILE"
}

write_profile_from_stock() {
  [[ -f "$STOCK_GDM_PROFILE" ]] || {
    echo "Packaged GDM profile not found: $STOCK_GDM_PROFILE" >&2
    exit 1
  }
  local tmp
  tmp="$(mktemp)"
  awk '
    function norm(s) {
      sub(/^[[:space:]]+/, "", s)
      sub(/[[:space:]]+$/, "", s)
      return s
    }
    {
      n = norm($0)
      if (n ~ /^system-db:gdm$/) {
        if (!inserted) {
          print
          inserted = 1
        }
        next
      }
      print
      if (!inserted && n ~ /^user-db:/) {
        print "system-db:gdm"
        inserted = 1
      }
    }
    END { if (!inserted) exit 1 }
  ' "$STOCK_GDM_PROFILE" >"$tmp"
  sudo mkdir -p /etc/dconf/profile
  # mktemp is mode 600. cp keeps that mode, and GDM (user gdm) then cannot
  # read the profile, so it never loads system-db:gdm.
  sudo install -m 644 "$tmp" "$GDM_PROFILE"
  rm -f "$tmp"
}

changed=0

if logind_ready; then
  echo "logind: already set (${LOGIND_DROPIN})"
else
  sudo mkdir -p /etc/systemd/logind.conf.d
  sudo cp "$SCRIPT_DIR/logind-server-mode.conf" "$LOGIND_DROPIN"
  sudo systemctl restart systemd-logind
  echo "logind: installed (${LOGIND_DROPIN})"
  changed=1
fi

gdm_db_changed=0
if gdm_keys_ready; then
  echo "GDM power: already set (${GDM_KEYFILE})"
else
  sudo mkdir -p /etc/dconf/db/gdm.d
  sudo cp "$SCRIPT_DIR/gdm-no-suspend-ac.conf" "$GDM_KEYFILE"
  echo "GDM power: installed (${GDM_KEYFILE})"
  gdm_db_changed=1
  changed=1
fi

gdm_profile_changed=0
if profile_ready; then
  echo "GDM profile: already loads system-db:gdm"
else
  write_profile_from_stock
  echo "GDM profile: installed (${GDM_PROFILE})"
  gdm_profile_changed=1
  changed=1
fi

if [[ "$gdm_db_changed" -eq 1 ]]; then
  sudo dconf update
fi
if [[ "$gdm_db_changed" -eq 1 || "$gdm_profile_changed" -eq 1 ]]; then
  # GDM's dconf daemon keeps the profile it read at start. Stop that
  # process only; the user session has its own daemon.
  sudo pkill --uid gdm --exact dconf-service || true
fi

if [[ "$changed" -eq 0 ]]; then
  echo "Already configured. Nothing to change."
fi
