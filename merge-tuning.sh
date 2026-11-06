#!/usr/bin/env bash
# Bring jarvis/tuning into main on this machine (fast-forward, both repos), switch the brain's
# planner to your ChatGPT login through the hub (Gemini as fallback), reinstall, test, push.
#
#   bash ~/Desktop/AI_Assistant/merge-tuning.sh
#
# Safe to run again. Nothing is deleted; .env gets a .bak-<time> copy first.
set -uo pipefail

D="$HOME/Desktop"; A="$D/AI_Assistant"; O="$D/ai-orchestrator"
TS="$(date +%Y%m%d-%H%M)"
export PATH="$HOME/.local/bin:$PATH"
export NODE_OPTIONS="${NODE_OPTIONS:+$NODE_OPTIONS }--dns-result-order=ipv4first"
export npm_config_fetch_retries=5 npm_config_fetch_retry_mintimeout=20000 npm_config_fetch_retry_maxtimeout=120000

say()  { printf '\n\033[1m== %s\033[0m\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m   %s\n' "$*"; WARNINGS+=("$*"); }
die()  { printf '\n\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
WARNINGS=()

# ---------------------------------------------------------------------------------------------
say "1. ChatGPT login for codex (the hub runs it)"
command -v codex >/dev/null || die "codex is not installed: npm i -g @openai/codex@latest, then run this again"
if codex login status >/dev/null 2>&1; then
  ok "codex is already logged in"
else
  echo "  Open the link it prints on any device, enter the code, sign in with ChatGPT:"
  codex login --device-auth || die "codex login did not finish; run this script again"
  codex login status >/dev/null 2>&1 && ok "codex logged in" || die "codex still not logged in"
fi

# ---------------------------------------------------------------------------------------------
say "2. Fast-forward main to jarvis/tuning (both repos)"
ff() {   # $1 repo dir, $2 the commit tuning must contain
  local r="$1"
  [ "$(git -C "$r" rev-parse --abbrev-ref HEAD)" = main ] || die "$r is not on main"
  git -C "$r" diff --quiet HEAD -- || die "$r has uncommitted changes to tracked files; nothing changed"
  git -C "$r" fetch origin main jarvis/tuning || die "git fetch failed in $r"
  git -C "$r" merge --ff-only origin/main >/dev/null 2>&1 || true
  if git -C "$r" merge-base --is-ancestor origin/jarvis/tuning HEAD; then
    ok "$(basename "$r"): already contains jarvis/tuning ($(git -C "$r" rev-parse --short HEAD))"
  else
    git -C "$r" merge --ff-only origin/jarvis/tuning || die "$r: jarvis/tuning is not a fast-forward of main; nothing changed"
    ok "$(basename "$r"): main -> $(git -C "$r" rev-parse --short HEAD)"
  fi
  git -C "$r" merge-base --is-ancestor "$2" HEAD || die "$r is missing $2"
}
ff "$A" f60568c
ff "$O" 9006e44

# ---------------------------------------------------------------------------------------------
say "3. Brain .env: planner = codex through the hub, Gemini as fallback"
cp -p "$A/.env" "$A/.env.bak-$TS" && ok "backup: $A/.env.bak-$TS"
python3 - "$A/.env" <<'PY' || die "could not update .env (the backup is untouched)"
import sys
path = sys.argv[1]
lines = open(path, encoding="utf-8").read().replace("\r", "").split("\n")
env = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in lines if "=" in l and not l.lstrip().startswith("#")}
gemini_keys = env.get("PLANNER_FALLBACK_API_KEYS") or env.get("PLANNER_API_KEYS") or env.get("PLANNER_API_KEY") or env.get("CLASSIFIER_API_KEYS", "")
if not gemini_keys:
    sys.exit("no Gemini keys found in PLANNER_API_KEYS / CLASSIFIER_API_KEYS")
want = {
    "PLANNER_BASE_URL": "hub",
    "PLANNER_MODEL": "codex",
    "PLANNER_API_KEY": "",
    "PLANNER_API_KEYS": "",          # the Gemini keys move to the fallback; the hub needs none on loopback
    "PLANNER_FALLBACK_BASE_URL": "https://generativelanguage.googleapis.com/v1beta/openai",
    "PLANNER_FALLBACK_API_KEYS": gemini_keys,
    "PLANNER_FALLBACK_MODEL": "gemini-3.8-flash",
}
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
    out += ["", "# planner through the Orchestrator hub (ChatGPT login), Gemini as fallback"] + [f"{k}={want[k]}" for k in missing] + [""]
open(path, "w", encoding="utf-8").write("\n".join(out))
print(f"  set {', '.join(want)} (Gemini keys: {len(gemini_keys.split(','))})")
PY
chmod 600 "$A/.env"

# ---------------------------------------------------------------------------------------------
say "4. Reinstall + restart: brain, then hub"
"$A/deploy/linux/install.sh" || die "brain install failed. Your old settings: $A/.env.bak-$TS"
"$O/deploy/linux/install.sh" || die "hub install failed: journalctl --user -u orchestrator-hub -n 50"

# ---------------------------------------------------------------------------------------------
say "5. Tests"
echo "  hub -> codex (one plain call, can take up to a minute):"
reply="$(curl -sS -m 240 http://127.0.0.1:4317/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"raw:codex","messages":[{"role":"user","content":"Reply with exactly: HUB-OK"}]}' 2>&1)"
if printf '%s' "$reply" | grep -q 'HUB-OK'; then ok "hub answered through codex"
else warn "hub/codex test failed: $(printf '%s' "$reply" | head -c 300)"; fi

echo "  brain planner (codex first, Gemini if that fails):"
if (cd "$A" && deploy/linux/run brain python - <<'PY'
import asyncio, logging
logging.basicConfig(level=logging.WARNING, format="  %(message)s")
from app.llm import LLM, planner_profile
async def main():
    p = planner_profile()
    print(f"  primary {p.model} at {p.base_url}; fallback {p.fallback.model if p.fallback else 'none'}")
    out = await LLM(p).complete([{"role": "user", "content": "Reply with exactly: BRAIN-OK"}], max_tokens=20)
    print("  reply:", out.strip()[:80])
    assert "BRAIN-OK" in out
asyncio.run(main())
PY
); then ok "brain planner works (a 'failed ... trying planner-fallback' line above means codex failed and Gemini answered)"
else warn "brain planner test failed (see above). Undo: cp $A/.env.bak-$TS $A/.env && systemctl --user restart assistant-brain"; fi

# ---------------------------------------------------------------------------------------------
say "6. Push main to GitHub"
for r in "$A" "$O"; do
  if git -C "$r" push origin main 2>/dev/null; then ok "$(basename "$r"): pushed"
  else warn "$(basename "$r"): push needs a GitHub login as Roger-Chris on this machine (gh auth login, or a token). Then: git -C $r push origin main"; fi
done

say "Done"
echo "Brain logs: journalctl --user -u assistant-brain -f | grep -i planner"
if [ ${#WARNINGS[@]} -gt 0 ]; then
  printf '\nStill to do:\n'; for w in "${WARNINGS[@]}"; do printf '  - %s\n' "$w"; done
fi
