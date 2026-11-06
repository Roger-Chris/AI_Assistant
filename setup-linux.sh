#!/usr/bin/env bash
# One-time setup of the Jarvis stack on this Linux machine (the always-on hub + brain).
#
#   bash ~/Desktop/AI_Assistant/setup-linux.sh [path/to/memory-seed]
#
# What it does, in order (safe to run again; nothing is deleted):
#   1. checks the tools (git, uv, Node 22.13+, tailscale)
#   2. shows which checkout the systemd services use now (the Desktop clones become the only ones)
#   3. backs up both .env files and both SQLite databases (.bak-<time>, online SQLite copy)
#   4. brain:  AI_Assistant/deploy/linux/update.sh   (git pull --ff-only, uv sync, units, restart)
#   5. hub:    ai-orchestrator/deploy/linux/update.sh (git pull --ff-only, npm ci, web build, restart)
#   6. checks both answer, and that the code is at least the 29 Oct merge (aeaca44 / ac8a085)
#   7. imports memory-seed/projects/*.md into shared memory, if the folder is on this machine
#   8. registers the `orc mcp` memory tools in Claude Code / Codex / Gemini CLI, where installed
set -uo pipefail

D="$HOME/Desktop"
A="$D/AI_Assistant"
O="$D/ai-orchestrator"
TS="$(date +%Y%m%d-%H%M)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"   # where uv installs itself

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m   %s\n' "$*"; WARNINGS+=("$*"); }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
WARNINGS=()

[ -d "$A/.git" ] || die "$A is not a git checkout"
[ -d "$O/.git" ] || die "$O is not a git checkout"

# ---------------------------------------------------------------------------------------------
say "1. Tools"
missing=0
command -v git >/dev/null && ok "$(git --version)" || { warn "git missing: sudo apt install -y git"; missing=1; }
command -v uv  >/dev/null && ok "uv $(uv --version | cut -d' ' -f2)" || { warn "uv missing: curl -LsSf https://astral.sh/uv/install.sh | sh   (then open a new terminal)"; missing=1; }
if command -v node >/dev/null; then
  if node -e 'const [a,b]=process.versions.node.split(".").map(Number); process.exit(a>22||(a===22&&b>=13)?0:1)'; then
    ok "node $(node --version)"
  else
    warn "node $(node --version) is too old (hub needs 22.13+): curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs"; missing=1
  fi
else
  warn "node missing: curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash - && sudo apt install -y nodejs"; missing=1
fi
command -v python3 >/dev/null || { warn "python3 missing: sudo apt install -y python3"; missing=1; }
if command -v tailscale >/dev/null; then
  ip4="$(tailscale ip -4 2>/dev/null | head -1 || true)"
  [ -n "$ip4" ] && ok "tailscale up ($ip4)" || warn "tailscale installed but not logged in: sudo tailscale up"
else
  warn "tailscale missing (the phone can't reach the hub without it): curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up"
fi
[ "$missing" = 0 ] || die "install the missing tools above, then run this script again"

# ---------------------------------------------------------------------------------------------
say "2. Which checkout the services use"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
for u in assistant-brain assistant-bridge orchestrator-hub; do
  f="$unit_dir/$u.service"
  if [ -f "$f" ]; then
    wd="$(grep -m1 '^WorkingDirectory=' "$f" | cut -d= -f2-)"
    echo "  $u -> $wd  ($(systemctl --user is-active "$u" 2>/dev/null))"
  else
    echo "  $u -> not installed yet"
  fi
done
echo "  The installers below point all three at $D/... from now on."
for old in "$HOME/AI_Assistant" "$HOME/Orchestrator"; do
  [ -d "$old" ] && echo "  $old is a second, unused clone (no data, no .env). Left as it is; remove it yourself once everything below works."
done

# ---------------------------------------------------------------------------------------------
say "3. Backups (.bak-$TS)"
for f in "$A/.env" "$O/.env" "$O/orchestrator.config.yaml"; do
  [ -f "$f" ] && cp -p "$f" "$f.bak-$TS" && ok "$f.bak-$TS"
done
backup_db() {   # online SQLite copy, safe while the service is writing
  [ -f "$1" ] || return 0
  python3 - "$1" "$1.bak-$TS" <<'PY' && ok "$1.bak-$TS" || warn "could not back up $1"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1]); dst = sqlite3.connect(sys.argv[2])
with dst: src.backup(dst)
dst.close(); src.close()
PY
}
backup_db "$A/data/brain/brain.db"
backup_db "$O/data/orchestrator.db"

# ---------------------------------------------------------------------------------------------
say "4. Brain (AI_Assistant): pull + install"
[ "$(git -C "$A" rev-parse --abbrev-ref HEAD)" = main ] || die "AI_Assistant is not on main: git -C $A switch main"
"$A/deploy/linux/update.sh" || die "brain update failed (see above). Logs: journalctl --user -u assistant-brain -n 50"

say "5. Hub (ai-orchestrator): pull + install"
[ "$(git -C "$O" rev-parse --abbrev-ref HEAD)" = main ] || die "ai-orchestrator is not on main: git -C $O switch main"
"$O/deploy/linux/update.sh" || die "hub update failed (see above). Logs: journalctl --user -u orchestrator-hub -n 50"

# ---------------------------------------------------------------------------------------------
say "6. Checks"
git -C "$A" merge-base --is-ancestor aeaca44 HEAD 2>/dev/null && ok "AI_Assistant at $(git -C "$A" rev-parse --short HEAD) (has the 29 Oct merge)" || warn "AI_Assistant is older than aeaca44"
git -C "$O" merge-base --is-ancestor ac8a085 HEAD 2>/dev/null && ok "ai-orchestrator at $(git -C "$O" rev-parse --short HEAD) (has the 29 Oct merge)" || warn "ai-orchestrator is older than ac8a085"
curl -fsS -m 5 http://127.0.0.1:8000/health >/dev/null && ok "brain answers on 127.0.0.1:8000" || warn "brain not answering: journalctl --user -u assistant-brain -n 50"
curl -fsS -m 5 http://127.0.0.1:4317/api/health >/dev/null && ok "hub answers on :4317" || warn "hub not answering: journalctl --user -u orchestrator-hub -n 50"
echo "  --- orc doctor ---"
(cd "$O" && npm run -s orc -- doctor 2>&1 | sed 's/^/  /') || warn "orc doctor reported a problem (see above)"
[ -f "$A/data/bridge-auth/creds.json" ] || warn "WhatsApp is not paired on this machine yet: systemctl --user stop assistant-bridge; cd $A && deploy/linux/run bridge  (scan the QR, wait for 'connected', Ctrl-C, systemctl --user start assistant-bridge)"

# ---------------------------------------------------------------------------------------------
say "7. Old projects -> shared memory"
SEED=""
for c in "${1:-}" "$D/memory-seed" "$HOME/memory-seed" "$HOME/Documents/memory-seed" "$HOME/Downloads/memory-seed"; do
  [ -n "$c" ] && [ -d "$c/projects" ] && { SEED="$(cd "$c" && pwd)"; break; }
done
if [ -n "$SEED" ]; then
  shopt -s nullglob; files=("$SEED"/projects/*.md); shopt -u nullglob
  if [ ${#files[@]} -gt 0 ]; then
    "$A/deploy/linux/run" brain python -m app.memory_import "${files[@]}" && ok "imported ${#files[@]} project notes from $SEED/projects" || warn "memory import failed (see above)"
    if [ -d "$SEED/people" ]; then
      shopt -s nullglob; people=("$SEED"/people/*.md); shopt -u nullglob
      [ ${#people[@]} -gt 0 ] && { "$A/deploy/linux/run" brain python -m app.memory_import --kind person "${people[@]}" && ok "imported ${#people[@]} people notes" || warn "people import failed"; }
    fi
  else
    warn "$SEED/projects has no .md files"
  fi
else
  warn "memory-seed not found on this machine. Copy C:\\development\\AI Assistant\\memory-seed from the laptop to ~/Desktop/memory-seed and run this script again (re-running only updates, never duplicates)"
fi

# ---------------------------------------------------------------------------------------------
say "8. Memory tools (orc mcp) for the AI CLIs on this machine"
mkdir -p "$HOME/.local/bin"
LAUNCH="$HOME/.local/bin/orc-mcp"
cat >"$LAUNCH" <<EOF
#!/usr/bin/env bash
# orc mcp needs the hub folder as its working directory (config + .env live there)
cd "$O" && exec "$O/node_modules/.bin/tsx" src/cli.ts mcp
EOF
chmod +x "$LAUNCH"
ok "launcher: $LAUNCH"
if command -v claude >/dev/null; then
  if claude mcp get orchestrator >/dev/null 2>&1; then ok "Claude Code: already registered"
  else claude mcp add --scope user orchestrator -- "$LAUNCH" >/dev/null 2>&1 && ok "Claude Code: registered" || warn "Claude Code: run  claude mcp add --scope user orchestrator -- $LAUNCH"; fi
else warn "claude CLI not installed here (npm i -g @anthropic-ai/claude-code, then claude /login)"; fi
if command -v codex >/dev/null; then
  if codex mcp list 2>/dev/null | grep -q '^orchestrator\b'; then ok "Codex: already registered"
  else codex mcp add orchestrator -- "$LAUNCH" >/dev/null 2>&1 && ok "Codex: registered" || warn "Codex: run  codex mcp add orchestrator -- $LAUNCH"; fi
else warn "codex CLI not installed here (npm i -g @openai/codex@latest, then codex login)"; fi
if command -v gemini >/dev/null; then
  if gemini mcp list 2>/dev/null | grep -q 'orchestrator'; then ok "Gemini CLI: already registered"
  else gemini mcp add --scope user orchestrator "$LAUNCH" >/dev/null 2>&1 && ok "Gemini CLI: registered" || warn "Gemini CLI: run  gemini mcp add --scope user orchestrator $LAUNCH"; fi
fi
command -v agy >/dev/null && echo "  Antigravity (agy): add $LAUNCH as an MCP server in its settings (no CLI command for it)."

# ---------------------------------------------------------------------------------------------
say "Done"
ip4="$(tailscale ip -4 2>/dev/null | head -1 || true)"
[ -n "$ip4" ] && echo "Phone app hub address:  http://$ip4:4317   (other devices: MEMORY_HUB=http://$ip4:4317 in their .env)"
echo "Status:  systemctl --user status assistant-brain assistant-bridge orchestrator-hub"
if [ ${#WARNINGS[@]} -gt 0 ]; then
  printf '\nStill to do:\n'; for w in "${WARNINGS[@]}"; do printf '  - %s\n' "$w"; done
fi
