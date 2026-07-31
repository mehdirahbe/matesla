#!/usr/bin/env bash
# Full SQLite history backup next to the live DB (not Dropbox — you copy elsewhere).
#
# Why full DB: TeslaFi history is minute-level and irreplaceable once gone from
# TeslaFi; lightweight exports would drop that fidelity.
#
# Policy:
#   - safe online snapshot (sqlite3 backup API — gunicorn can keep running)
#   - zstd -3 compression (~8× smaller on current ~635 MiB DB)
#   - keep the 4 newest archives only (~1 month if run weekly)
#   - skip if a backup younger than 6 days exists (unless FORCE=1)
#
# Usage:
#   ./scripts/backup_db.sh
#   FORCE=1 ./scripts/backup_db.sh          # ignore 6-day gap
#   KEEP=4 MIN_AGE_DAYS=6 ./scripts/backup_db.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="${MATESLA_DB:-$ROOT/db.sqlite3}"
BACKUP_DIR="${MATESLA_BACKUP_DIR:-$ROOT/db-backups}"
PYTHON="${MATESLA_PYTHON:-$ROOT/.venv/bin/python}"
KEEP="${KEEP:-4}"
MIN_AGE_DAYS="${MIN_AGE_DAYS:-6}"
FORCE="${FORCE:-0}"
ZSTD_LEVEL="${ZSTD_LEVEL:-3}"

if [[ ! -f "$DB_PATH" ]]; then
  echo "DB not found: $DB_PATH" >&2
  exit 1
fi
if [[ ! -x "$PYTHON" ]]; then
  echo "Python venv not found: $PYTHON" >&2
  exit 1
fi
if ! command -v zstd >/dev/null 2>&1; then
  echo "zstd not installed (apt install zstd)" >&2
  exit 1
fi

mkdir -p "$BACKUP_DIR"

# Already have a recent archive? Keep weekly cadence without spam.
if [[ "$FORCE" != "1" ]]; then
  newest="$(
    find "$BACKUP_DIR" -maxdepth 1 -type f -name 'matesla-db-*.sqlite3.zst' \
      -printf '%T@ %p\n' 2>/dev/null | sort -nr | head -1 | cut -d' ' -f2- || true
  )"
  if [[ -n "${newest:-}" ]]; then
    age_days=$(( ( $(date +%s) - $(stat -c %Y "$newest") ) / 86400 ))
    if (( age_days < MIN_AGE_DAYS )); then
      echo "Skip: newest backup is ${age_days}d old (< ${MIN_AGE_DAYS}d): $newest"
      echo "Use FORCE=1 to override."
      exit 0
    fi
  fi
fi

stamp="$(date +%Y-%m-%d)"
tmp_db="$(mktemp "$BACKUP_DIR/.matesla-db-XXXXXX.sqlite3")"
out_zst="$BACKUP_DIR/matesla-db-${stamp}.sqlite3.zst"
# Same day re-run: unique suffix so we never overwrite silently
if [[ -e "$out_zst" ]]; then
  stamp="$(date +%Y-%m-%d_%H%M%S)"
  out_zst="$BACKUP_DIR/matesla-db-${stamp}.sqlite3.zst"
fi

cleanup() {
  rm -f "$tmp_db" "${tmp_db}.zst" 2>/dev/null || true
}
trap cleanup EXIT

echo "Snapshot (online-safe) → $tmp_db"
"$PYTHON" - "$DB_PATH" "$tmp_db" <<'PY'
import sqlite3, sys
src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(f"file:{src_path}?mode=ro", uri=True)
dst = sqlite3.connect(dst_path)
with dst:
    src.backup(dst)
src.close()
dst.close()
print("backup_ok")
PY

echo "Compress zstd -${ZSTD_LEVEL} → $out_zst"
zstd -"${ZSTD_LEVEL}" -f "$tmp_db" -o "$out_zst"
rm -f "$tmp_db"
trap - EXIT

raw_mb="$(du -h "$DB_PATH" | cut -f1)"
zst_mb="$(du -h "$out_zst" | cut -f1)"
echo "Done: live DB ${raw_mb} → archive ${zst_mb}"
echo "  $out_zst"

# Keep only the N newest archives (by mtime)
mapfile -t all_backups < <(
  find "$BACKUP_DIR" -maxdepth 1 -type f -name 'matesla-db-*.sqlite3.zst' \
    -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-
)
if (( ${#all_backups[@]} > KEEP )); then
  for old in "${all_backups[@]:KEEP}"; do
    echo "Prune (keep ${KEEP}): $old"
    rm -f "$old"
  done
fi

echo "Archives in $BACKUP_DIR:"
ls -lh "$BACKUP_DIR"/matesla-db-*.sqlite3.zst 2>/dev/null || true
