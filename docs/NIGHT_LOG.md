# Night build log: finishing the assistant (2026-10-28 → 29)

What was done overnight, what is still running, and what is broken. Newest entries at the
bottom of each section. Each phase agent also keeps its own detailed log in `docs/night-log/`
on its branch (merged into `main` when the phase is merged).

## Ground rules for tonight

- **No accounts, no passwords.** Claude does not sign in to Google, Tailscale or any service with
  the owner's credentials and does not create accounts. Everything that needs a key is built
  switched off, with the exact setting to fill in listed under "Needs you" below.
- **Change the Google password** that was pasted into the chat on 2026-10-28.
- Code lands on `night/*` branches first; it is merged into `main` (and pushed) only after its
  tests pass. `main` stays deployable.
- The Linux machine is not reachable from this laptop's tools, so nothing was run there.
  After pulling, run `deploy/linux/update.sh` in both repos.

## Status

| Phase | What | Branch | State |
|---|---|---|---|
| 1 | Quiet by default: alert levels, quiet hours, hub delivery, Done / Snooze / Not important | `main` (57275cb, ade0737) | done, pushed |
| 1 test | Emulator end-to-end test of Phase 1 (test brain + test hub on this laptop, test tokens) | n/a | in progress |
| 2 + 4 | Watcher (deadlines, promises, bills, who is waiting, system health), escalation, learning from "Not important"; weekly review, renewals, monthly report | `night/phase2-4` (AI_Assistant) | agent running |
| 3 | Alarm screen + two-way voice call in the app (Groq Whisper + model with safe actions) | `night/phase3-call` (Orchestrator) | agent running |
| 5 | Looking after itself: health pings, ntfy fallback, nightly backups | merged into `main` | done: 76 tests pass, verified before merge; untested on real Linux (restic, rclone, timer, ntfy, healthchecks) |

**Update 2026-10-29 (morning): all phases merged.**

- **Phases 2 + 4** merged into AI_Assistant `main` (52a5a88): 111 brain tests pass after the merge,
  ruff clean. New: `brain/app/watcher.py` (deadlines at risk, promises, bills, renewals, who is
  waiting ranked by the memory graph, WhatsApp silence / Gmail errors, alarm → call escalation
  capped at `CALLS_PER_DAY`, learning from "Not important") and `brain/app/review.py` (Sunday
  weekly review, monthly noise report, Gmail draft replies, off by default). Details:
  `docs/night-log/phase2-4.md`.
- **Phase 3** merged into Orchestrator `main` (5b3ad8b) after merging in the other session's 4 new
  commits (2 conflicts, both sides kept): 102 hub tests pass, hub/web/mobile typecheck clean.
  New: loud "alarm" channel for level 3-4, call screen (`mobile/src/app/call/[id].tsx`: ring,
  speak, push-to-talk, typed fallback), `POST /api/calls/:alertId/turn` (Groq Whisper + a fixed
  list of safe actions; draft/delegate only after you say yes). New native packages `expo-audio`
  and `expo-speech`: the next APK needs `npx expo prebuild` (the GitHub release workflow does a
  full build). Not built: full-screen intent (Expo has no option for it with notification
  messages). Untested on a device: channel sound/DND on HyperOS, recording, TTS, real Groq.
  Details: `docs/night-log/phase3-call.md`.
- Still needs you, on top of the list below: `GROQ_API_KEY(S)` in the hub's `.env` for voice;
  optionally `WATCH_VIP_CHATS` in the brain's `.env`; on the phone allow the microphone and, if
  HyperOS did not grant it, "Override Do Not Disturb" for the "Alarms and calls" channel.

**Stopped early (usage limit, superseded by the update above).** Phases 2+4 and 3 were still being built by agents when the session
hit its limit. Whatever they committed is on their branches (`night/phase2-4` in AI_Assistant,
`night/phase3-call` in Orchestrator, if pushed) and in their worktrees under
`C:\development\AI Assistant\worktrees\`. Not merged, not verified. Next session: check each
branch's `docs/night-log/*.md`, run its tests, then merge (expect a small conflict in
`brain/app/scheduler.py` between Phase 2+4 and Phase 5).

**Emulator test, not finished.** The app built and installed fine, but the `Pixel_7` emulator has a
screen-lock PIN (unknown), so the app could not be opened; the second emulator (`Pixel_7_2`) was
still booting. Next time: create a fresh AVD with no lock, `adb -s emulator-5554 reverse tcp:4399
tcp:4399`, and connect the app to `http://127.0.0.1:4399` with the test hub's token from the
scratch config. The brain → hub part already passed.

## Needs you (accounts and keys only you can set up)

From Phase 5 (details in `deploy/linux/README.md`, section 5):
1. **ntfy:** install the ntfy app, make a topic (`openssl rand -hex 16`), subscribe, set
   `NTFY_TOPIC` (and optionally `NTFY_CLICK_URL=http://<hub tailscale ip>:4317`) in `.env`.
2. **healthchecks.io:** add a check (5 min period, 15 min grace), put its ping URL in
   `HEALTHCHECK_URL`, turn on email alerts.
3. **Backups:** `sudo apt install restic rclone sqlite3`; `rclone config` with a Drive remote named
   `gdrive`; `rclone mkdir gdrive:assistant-backup`; restic password in
   `~/.config/assistant/restic-password` (chmod 600, keep a copy elsewhere);
   `BACKUP_REPO=rclone:gdrive:assistant-backup`; then `deploy/linux/install.sh` and
   `deploy/linux/backup.sh` once.
4. **Change the Google password** pasted into the chat.

## Log

### Phase 1 end-to-end test (laptop)

- Test brain on 127.0.0.1:8099 and test hub on 127.0.0.1:4399, from a clean worktree of the
  pushed hub code, with fresh test tokens and empty databases in the session scratchpad. Your real
  `.env`, data and tokens were not used. AI CLIs, cluster and memory are off in the test hub.
- Brain → hub: three alerts raised through `alerts.raise_alert` (brief, form due today, Jayesh
  waiting) arrived at the hub's `POST /api/notify` and are listed on `GET /api/home` (most urgent
  first). PASS.
- Found while testing: a `.env` value with a space breaks `set -a; . file` in a shell. The brain's
  `deploy/linux/env.sh` already reads files safely; only ad-hoc shell sourcing is affected.
