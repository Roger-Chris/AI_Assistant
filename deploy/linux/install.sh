#!/usr/bin/env bash
# Sets up (or refreshes) the brain and the WhatsApp bridge as systemd user services.
# Safe to run again at any time: it re-syncs dependencies, rewrites the units and restarts.
# Prerequisites and the full walk-through: deploy/linux/README.md
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/env.sh"

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }

say "Checking tools"
case "$REPO" in *[[:space:]]*) die "the repo path has a space in it ($REPO); clone it somewhere without one" ;; esac
command -v systemctl >/dev/null || die "systemd is required"
command -v git >/dev/null || die "git is missing: sudo apt install git"
command -v uv >/dev/null || die "uv is missing: curl -LsSf https://astral.sh/uv/install.sh | sh  (then open a new shell)"
NODE="$(command -v node || true)"
[ -n "$NODE" ] || die "node is missing: install Node 22 LTS (deploy/linux/README.md)"
node_major="$("$NODE" -p 'process.versions.node.split(".")[0]')"
[ "$node_major" -ge 20 ] || die "Node $node_major is too old: the bridge needs 20+, the Orchestrator hub 22.13+"
echo "uv $(uv --version | cut -d' ' -f2), node $("$NODE" --version), $(git --version)"

say "Checking .env"
if [ ! -f "$REPO/.env" ]; then
  cp "$REPO/.env.example" "$REPO/.env"
  chmod 600 "$REPO/.env"
  die "created .env from .env.example. Fill in BRAIN_TOKEN, OWNER_NAME and the API keys (or copy your old .env over it), then run this again"
fi
sed -i 's/\r$//' "$REPO/.env"   # a .env copied from Windows has CRLF endings; systemd would keep the \r
chmod 600 "$REPO/.env"
if grep -q '^BRAIN_TOKEN=change-me' "$REPO/.env"; then
  die "set BRAIN_TOKEN in .env (openssl rand -hex 24)"
fi

say "Data folders"
mkdir -p "$REPO"/data/brain/{media,google,profile} "$REPO/data/bridge-auth"
chmod 700 "$REPO/data"

say "Brain: Python environment (brain/.venv)"
(cd "$REPO/brain" && uv sync --python 3.12)

say "Bridge: npm packages"
(cd "$REPO/bridge" && npm install --omit=dev --no-audit --no-fund)

say "systemd user units"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
for name in assistant-brain assistant-bridge; do
  sed -e "s|@REPO@|$REPO|g" -e "s|@NODE@|$NODE|g" -e "s|@PATH@|$PATH|g" \
    "$REPO/deploy/linux/$name.service.in" >"$unit_dir/$name.service"
  echo "wrote $unit_dir/$name.service"
done

# Nightly backups (backup.sh), only when BACKUP_REPO is set in .env; otherwise removed again.
backup_repo="$(load_env "$REPO/.env"; printf '%s' "${BACKUP_REPO:-}")"
if [ -n "$backup_repo" ]; then
  for name in assistant-backup.service assistant-backup.timer; do
    sed -e "s|@REPO@|$REPO|g" -e "s|@PATH@|$PATH|g" "$REPO/deploy/linux/$name.in" >"$unit_dir/$name"
    echo "wrote $unit_dir/$name"
  done
  command -v restic >/dev/null || echo "warning: restic is missing (sudo apt install restic); backups will fail until it is installed"
else
  if [ -f "$unit_dir/assistant-backup.timer" ]; then
    systemctl --user disable --now assistant-backup.timer 2>/dev/null || true
    rm -f "$unit_dir/assistant-backup.timer" "$unit_dir/assistant-backup.service"
    echo "BACKUP_REPO is empty: removed the backup timer"
  fi
fi
systemctl --user daemon-reload
if [ -n "$backup_repo" ]; then
  systemctl --user enable --now assistant-backup.timer >/dev/null
  echo "backups: nightly at 03:45 to $backup_repo (systemctl --user list-timers assistant-backup.timer)"
fi

# Without lingering, user services stop at logout and don't start at boot.
if ! loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q 'Linger=yes'; then
  say "Letting your services run without a login (loginctl enable-linger)"
  loginctl enable-linger "$USER" 2>/dev/null || sudo loginctl enable-linger "$USER"
fi

say "Starting the brain"
systemctl --user enable assistant-brain >/dev/null
systemctl --user restart assistant-brain
up=no
for _ in $(seq 30); do
  if "$REPO/brain/.venv/bin/python" -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)" 2>/dev/null; then
    up=yes
    break
  fi
  sleep 2
done
[ "$up" = yes ] || die "the brain did not answer on :8000 within a minute: journalctl --user -u assistant-brain -n 50"
echo "brain is up: http://127.0.0.1:8000/health"

systemctl --user enable assistant-bridge >/dev/null
if [ -f "$REPO/data/bridge-auth/creds.json" ]; then
  say "Starting the bridge (WhatsApp session found)"
  systemctl --user restart assistant-bridge
  echo "bridge started: journalctl --user -u assistant-bridge -f"
else
  say "The bridge is not paired with WhatsApp yet"
  cat <<EOF
Pair it once in the foreground:
  deploy/linux/run bridge
Scan the QR code from WhatsApp -> Linked devices, wait for "connected", press Ctrl-C, then:
  systemctl --user start assistant-bridge
EOF
fi

say "Done"
echo "Logs:    journalctl --user -u assistant-brain -f   (or assistant-bridge)"
echo "Update:  deploy/linux/update.sh"
[ -z "$backup_repo" ] || echo "Backup:  deploy/linux/backup.sh (now), journalctl --user -u assistant-backup"
