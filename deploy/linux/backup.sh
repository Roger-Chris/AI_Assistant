#!/usr/bin/env bash
# Nightly encrypted, deduplicated backup with restic. Run by assistant-backup.timer (03:45), or by
# hand: deploy/linux/backup.sh. Restore steps: deploy/linux/restore.md
#
# What goes in:
#   this repo:          data/ (minus live SQLite files), .env
#   the Orchestrator:   data/ (minus live SQLite files), .env, fcm-service-account.json, gmail-client.json
#   every *.db / *.sqlite under those data/ folders, copied with SQLite's online backup first
#   (a raw copy of a database that is being written can be torn), into $STAGE below.
#
# Settings, from .env:
#   BACKUP_REPO            restic repository: rclone:gdrive:assistant-backup, s3:..., /mnt/disk/restic
#   BACKUP_PASSWORD_FILE   file holding the restic password (default ~/.config/assistant/restic-password)
#   ORCHESTRATOR_DIR       the Orchestrator checkout (default ../Orchestrator or ../ai-orchestrator)
#   S3/R2 keys, if the repo is S3: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"
load_env "$REPO/.env"

STATUS="$REPO/data/brain/backup-status"   # read by the brain's health check
STAGE="${XDG_CACHE_HOME:-$HOME/.cache}/assistant-backup/sqlite"

say() { printf '== %s\n' "$*"; }
status() { mkdir -p "$(dirname "$STATUS")"; printf '%s %s\n' "$1" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$STATUS"; }
die() { printf 'backup failed: %s\n' "$*" >&2; status failed; exit 1; }
trap 'status failed' ERR

[ -n "${BACKUP_REPO:-}" ] || { echo "BACKUP_REPO is not set in .env; nothing to do"; exit 0; }
export RESTIC_REPOSITORY="$BACKUP_REPO"
export RESTIC_PASSWORD_FILE="${BACKUP_PASSWORD_FILE:-$HOME/.config/assistant/restic-password}"
[ -s "$RESTIC_PASSWORD_FILE" ] || die "no restic password at $RESTIC_PASSWORD_FILE (see deploy/linux/README.md, Backups)"
command -v restic >/dev/null || die "restic is missing: sudo apt install restic"
case "$BACKUP_REPO" in rclone:*) command -v rclone >/dev/null || die "rclone is missing: sudo apt install rclone" ;; esac

ORCH="${ORCHESTRATOR_DIR:-}"
if [ -z "$ORCH" ]; then
  for d in "$REPO/../Orchestrator" "$REPO/../ai-orchestrator"; do
    [ -d "$d/data" ] && { ORCH="$(cd "$d" && pwd)"; break; }
  done
fi

# One online-backup copy of a SQLite database. sqlite3's .backup if installed, else Python's API.
snapshot_db() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  rm -f "$dst"
  if command -v sqlite3 >/dev/null; then
    sqlite3 "$src" ".timeout 10000" ".backup '$dst'"
  else
    python3 - "$src" "$dst" <<'PY'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1], timeout=10)
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close(); src.close()
PY
  fi
}

# name, data folder -> snapshots of its databases under $STAGE/name/
snapshot_all() {
  local name="$1" data="$2" db
  [ -d "$data" ] || return 0
  while IFS= read -r -d '' db; do
    snapshot_db "$db" "$STAGE/$name/${db#"$data"/}"
    echo "  $name: ${db#"$data"/}"
  done < <(find "$data" -path "$STAGE" -prune -o -type f \( -name '*.db' -o -name '*.sqlite' \) -print0)
}

say "Copying databases safely into $STAGE"
rm -rf "$STAGE"
mkdir -p "$STAGE"
chmod 700 "$(dirname "$STAGE")"
snapshot_all ai_assistant "$REPO/data"
[ -n "$ORCH" ] && snapshot_all orchestrator "$ORCH/data"

paths=("$REPO/data" "$STAGE")
[ -f "$REPO/.env" ] && paths+=("$REPO/.env")
if [ -n "$ORCH" ]; then
  for f in data .env fcm-service-account.json gmail-client.json; do
    [ -e "$ORCH/$f" ] && paths+=("$ORCH/$f")
  done
  echo "Orchestrator: $ORCH"
else
  echo "Orchestrator: not found (set ORCHESTRATOR_DIR in .env to include it)"
fi

if ! restic cat config >/dev/null 2>&1; then
  say "Creating the restic repository at $BACKUP_REPO"
  restic init
fi

say "restic backup"
# live databases (and their -wal/-shm/-journal) are skipped: their safe copies are in $STAGE
restic backup --tag nightly --one-file-system \
  --exclude '*.db' --exclude '*.db-wal' --exclude '*.db-shm' --exclude '*.db-journal' \
  --exclude '*.sqlite' --exclude '*.sqlite-wal' --exclude '*.sqlite-shm' --exclude '*.sqlite-journal' \
  "${paths[@]}"

say "restic forget --prune (7 daily, 4 weekly, 6 monthly)"
restic forget --prune --tag nightly --keep-daily 7 --keep-weekly 4 --keep-monthly 6

if [ "$(date +%u)" = 7 ]; then
  say "Sunday: restic check"
  restic check
fi

rm -rf "$STAGE"
status ok
say "Backup done"
