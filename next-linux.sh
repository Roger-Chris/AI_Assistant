#!/usr/bin/env bash
# Move the Linux hub to jarvis/next and finish its setup (steps 1-6 of the 30 Oct list):
#   1. both repos on jarvis/next, reinstalled and restarted
#   2. codex logged in with ChatGPT (device code)
#   3. brain thinks through the hub: PLANNER_BASE_URL=hub, PLANNER_MODEL=auto (Gemini as fallback)
#   4. hub voice: GEMINI_API_KEY (one of your existing Gemini keys) unless a voice key is already set
#   5. Android builds go to the laptop: builds.androidNode in the hub config
#   6. optional: the live evaluation (asks first; it takes a while)
#
#   bash ~/Desktop/AI_Assistant/next-linux.sh
#
# Safe to run again. Nothing is deleted; .env files and the config get .bak-<time> copies first.
set -uo pipefail

D="$HOME/Desktop"; A="$D/AI_Assistant"; O="$D/ai-orchestrator"
TS="$(date +%Y%m%d-%H%M)"
ANDROID_NODE="${ANDROID_NODE:-omen}"      # the laptop's device name in the hub (Devices tab)
export PATH="$HOME/.local/bin:$PATH"
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first"
export npm_config_fetch_retries=5 npm_config_fetch_retry_mintimeout=20000 npm_config_fetch_retry_maxtimeout=120000

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m   %s\n' "$*"; WARNINGS+=("$*"); }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
WARNINGS=()

say "0. Network"
node -e "fetch('https://registry.npmjs.org/',{signal:AbortSignal.timeout(15000)}).then(()=>process.exit(0),e=>{console.error('  '+(e.cause?.code||e.message));process.exit(1)})" \
  && ok "npm registry reachable" || die "no internet for Node/npm; nothing changed"

# ---------------------------------------------------------------------------------------------
say "1a. Backups (.bak-$TS)"
for f in "$A/.env" "$O/.env" "$O/orchestrator.config.yaml"; do cp -p "$f" "$f.bak-$TS" && ok "$f.bak-$TS"; done
for db in "$A/data/brain/brain.db" "$O/data/orchestrator.db"; do
  [ -f "$db" ] && python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" "$db" "$db.bak-$TS" && ok "$db.bak-$TS"
done

say "1b. Both repos -> jarvis/next"
to_next() {
  local r="$1"
  git -C "$r" diff --quiet HEAD -- || die "$r has uncommitted changes to tracked files; nothing changed there"
  git -C "$r" fetch origin jarvis/next || die "git fetch failed in $r"
  if git -C "$r" show-ref --verify --quiet refs/heads/jarvis/next; then
    git -C "$r" switch jarvis/next >/dev/null 2>&1 || die "could not switch $r to jarvis/next"
    git -C "$r" merge --ff-only origin/jarvis/next >/dev/null || die "$r: local jarvis/next has diverged from GitHub"
  else
    git -C "$r" switch -c jarvis/next --track origin/jarvis/next >/dev/null 2>&1 || die "could not switch $r to jarvis/next"
  fi
  ok "$(basename "$r"): jarvis/next at $(git -C "$r" rev-parse --short HEAD) (main is untouched; back with: git -C $r switch main)"
}
to_next "$A"
to_next "$O"

# ---------------------------------------------------------------------------------------------
say "2. codex (ChatGPT login)"
if ! command -v codex >/dev/null; then
  warn "codex not installed: npm i -g @openai/codex@latest, then run this again (the planner falls back to Gemini meanwhile)"
elif codex login status >/dev/null 2>&1; then
  ok "codex already logged in"
else
  echo "  Open the link it prints on any device, enter the code, sign in with ChatGPT:"
  codex login --device-auth && ok "codex logged in" || warn "codex login did not finish; run: codex login --device-auth"
fi

# ---------------------------------------------------------------------------------------------
say "3+4. .env changes"
python3 - "$A/.env" "$O/.env" <<'PY' || die "could not edit the .env files (backups are untouched)"
import sys

def load(path):
    lines = open(path, encoding="utf-8").read().replace("\r", "").split("\n")
    env = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in lines if "=" in l and not l.lstrip().startswith("#")}
    return lines, env

def write(path, lines, want, note):
    out, seen = [], set()
    for l in lines:
        k = l.split("=", 1)[0] if "=" in l and not l.lstrip().startswith("#") else None
        if k in want:
            out.append(f"{k}={want[k]}"); seen.add(k)
        else:
            out.append(l)
    missing = [k for k in want if k not in seen]
    if missing:
        while out and out[-1] == "":
            out.pop()
        out += ["", f"# {note}"] + [f"{k}={want[k]}" for k in missing] + [""]
    open(path, "w", encoding="utf-8").write("\n".join(out))

brain, hub = sys.argv[1], sys.argv[2]
bl, be = load(brain)
gemini = be.get("PLANNER_FALLBACK_API_KEYS") or be.get("PLANNER_API_KEYS") or be.get("PLANNER_API_KEY") or be.get("CLASSIFIER_API_KEYS", "")
if not gemini:
    sys.exit("no Gemini keys in the brain's .env")
write(brain, bl, {
    "PLANNER_BASE_URL": "hub",
    "PLANNER_MODEL": "auto",
    "PLANNER_API_KEY": "",
    "PLANNER_API_KEYS": "",
    "PLANNER_FALLBACK_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
    "PLANNER_FALLBACK_API_KEYS": gemini,
    "PLANNER_FALLBACK_MODEL": "gemini-3.8-flash",
}, "planner through the Orchestrator hub (its model router), Gemini as fallback")
print(f"  brain: PLANNER_BASE_URL=hub, PLANNER_MODEL=auto, Gemini fallback ({len(gemini.split(','))} keys)")

hl, he = load(hub)
if he.get("OPENAI_API_KEY") or he.get("GEMINI_API_KEY") or he.get("GOOGLE_API_KEY"):
    print("  hub: a voice key is already set, left as it is")
else:
    write(hub, hl, {"GEMINI_API_KEY": gemini.split(",")[0].strip()}, "hub voice (tts); one of the brain's Gemini keys")
    print("  hub: GEMINI_API_KEY = the first of the brain's Gemini keys (voice)")
PY
chmod 600 "$A/.env" "$O/.env"

# ---------------------------------------------------------------------------------------------
say "5. Android builds on the laptop"
python3 - "$O/orchestrator.config.yaml" "$ANDROID_NODE" <<'PY' || die "could not edit orchestrator.config.yaml (backup is untouched)"
import re, sys
path, node = sys.argv[1], sys.argv[2]
src = open(path, encoding="utf-8").read()
if re.search(r"^builds:", src, re.M):
    if re.search(r"^\s+androidNode:", src, re.M):
        src = re.sub(r"^(\s+androidNode:).*$", rf'\1 "{node}"', src, count=1, flags=re.M)
    else:
        src = re.sub(r"^builds:.*\n", lambda m: m.group(0) + f'  androidNode: "{node}"\n', src, count=1, flags=re.M)
else:
    block = f'builds:\n  androidNode: "{node}"          # the device with the Android SDK (it needs builds.androidHere: true)\n\n'
    src = src.replace("cluster:\n", block + "cluster:\n", 1) if "\ncluster:\n" in src else src.rstrip("\n") + "\n\n" + block
open(path, "w", encoding="utf-8").write(src)
print(f'  builds.androidNode: "{node}"')
PY

# ---------------------------------------------------------------------------------------------
say "Reinstall + restart: brain, then hub"
"$A/deploy/linux/install.sh" || die "brain install failed. Undo: cp $A/.env.bak-$TS $A/.env; git -C $A switch main; $A/deploy/linux/install.sh"
"$O/deploy/linux/install.sh" || die "hub install failed: journalctl --user -u orchestrator-hub -n 50"

# ---------------------------------------------------------------------------------------------
say "Checks"
curl -fsS -m 5 http://127.0.0.1:8000/health >/dev/null && ok "brain answers" || warn "brain not answering: journalctl --user -u assistant-brain -n 50"
curl -fsS -m 5 http://127.0.0.1:4317/api/health >/dev/null && ok "hub answers" || warn "hub not answering: journalctl --user -u orchestrator-hub -n 50"
echo "  devices the hub sees (builds.androidNode must be one of these names):"
(cd "$O" && npm run -s orc -- nodes 2>&1 | sed 's/^/    /') || true
echo "  hub model router (raw:auto), one short call:"
reply="$(curl -sS -m 240 http://127.0.0.1:4317/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"raw:auto","messages":[{"role":"user","content":"Reply with exactly: HUB-OK"}]}' 2>&1)"
printf '%s' "$reply" | grep -q 'HUB-OK' && ok "hub answered" || warn "hub model test failed: $(printf '%s' "$reply" | head -c 300)"
echo "  brain planner through the hub:"
if (cd "$A" && deploy/linux/run brain python - <<'PY'
import asyncio, logging
logging.basicConfig(level=logging.WARNING, format="    %(message)s")
from app.llm import LLM, planner_profile
async def main():
    p = planner_profile()
    out = await LLM(p).complete([{"role": "user", "content": "Reply with exactly: BRAIN-OK"}], max_tokens=20)
    print(f"    {p.model} -> {out.strip()[:60]}")
    assert "BRAIN-OK" in out
asyncio.run(main())
PY
); then ok "brain planner works (a 'trying planner-fallback' line above means the hub failed and Gemini answered)"
else warn "brain planner test failed. Undo the planner change: cp $A/.env.bak-$TS $A/.env && systemctl --user restart assistant-brain"; fi

# ---------------------------------------------------------------------------------------------
say "6. Live evaluation (real model, fake devices, throwaway database)"
EVAL="cd $A && deploy/linux/run brain python -m app.agent_eval --model codex --hub http://127.0.0.1:4317 --out $A/eval.json"
read -r -t 60 -p "  Run it now? It can take 30-60 minutes. [y/N] " yn || yn=n
if [[ "$yn" =~ ^[Yy] ]]; then
  (eval "$EVAL") 2>&1 | tee "$A/eval-$TS.log"
  [ -f "$A/eval.json" ] && ok "report: $A/eval.json (log: $A/eval-$TS.log)"
else
  echo "  Later:  $EVAL"
fi

say "Done"
echo "Laptop: set  builds.androidHere: true  in its orchestrator.config.yaml and restart its hub."
if [ ${#WARNINGS[@]} -gt 0 ]; then
  printf '\nStill to do:\n'; for w in "${WARNINGS[@]}"; do printf '  - %s\n' "$w"; done
fi
