# AI_Assistant — working notes for Claude

Personal Jarvis-style assistant for **Roger Christopher**. Reads WhatsApp and Gmail, turns real
commitments into tracked tasks and calendar events, and reaches the phone. Owner name and aliases
live in `.env` (`OWNER_NAME`, `OWNER_ALIASES`).

## Layout

```
brain/    always-on orchestrator. FastAPI + SQLModel(SQLite) + APScheduler. CPU-only, no GPU deps.
bridge/   WhatsApp linked-device bridge (Baileys, Node 20). READ-ONLY: no send path exists.
worker/   GPU / laptop-only jobs (Whisper, local model). Heartbeat stub only so far — Phase 4.
android/  the phone app. Not started. Telegram is the interim remote.
data/     git-ignored: brain.db, WhatsApp session, media, Google tokens, the CV. Never commit.
```

**The rule that keeps moving hosts trivial: nothing in `brain/`, `bridge/` or
`docker-compose.yml` may depend on a GPU or on this laptop.** Heavy work goes in `worker/` and
talks to the brain over HTTP.

Hosts: the permanent host is a **Linux machine** (8 GB RAM, hard disk, no GPU), running the brain
and bridge **natively as systemd user services, no Docker** (`deploy/linux/`, see its README). The
brain listens on 127.0.0.1:8000 only; the Orchestrator hub (`../Orchestrator`) runs on the same
machine and is the phone's way in. The HP Omen is for development (Docker Compose) and is a GPU
worker when it is on. The Mac mini joins later as an Orchestrator coding device, not as this
project's host. GPU jobs beyond the Omen go to free cloud notebooks (Colab/Kaggle) on demand.

## Commands

On the Linux machine (native, systemd):

```bash
deploy/linux/install.sh                       # deps, units, (re)start; safe to re-run
deploy/linux/update.sh                        # git pull --ff-only + install
journalctl --user -u assistant-brain -f       # follow (or assistant-bridge)
deploy/linux/run brain python -m app.google_auth [label]   # any command with the services' env
```

`deploy/linux/native.env` overrides the Docker paths in `.env` (relative to brain/ and bridge/).
Keep it in step with `docker-compose.yml` when paths change.

On the laptop (Docker):

```bash
docker compose up --build -d brain bridge   # rebuild + restart
docker compose logs -f brain                # follow
cd brain && uv run --extra dev pytest -q    # 76 tests, all offline (fake LLM + mocked HTTP)
cd brain && ruff check app tests
docker compose run --rm -p 8765:8765 brain python -m app.google_auth [label]   # OAuth per account
docker compose run --rm brain python -m app.seed_profile /data/profile/resume.docx
```

With Docker, read the database while the container runs with **read-only** sqlite
(`sqlite3.connect('file:data/brain/brain.db?mode=ro', uri=True)`). Writing to it from outside the
container gives "disk I/O error". Natively on Linux this does not apply.

## How a message flows

`ingest` → muted-chat check → `perceive` (voice notes transcribed, images described) →
`prefilter` (rule-based, free) → `classifier` (cheap model) → notify.

The classifier is **task-aware**: it sees the chat's open tasks plus recent open tasks from the
same source, and returns an action — `new` / `update` / `complete` / `none`. That is what stops
"can you send X" + "I'll send in 10 min" becoming two cards, and lets a confirmation mail close a
registration task even from a different sender.

Questions to the owner become `watching` tasks: silent unless unanswered past
`QUESTION_GRACE_MINUTES`, and closed silently by any reply from him in that chat.

## Alerts: quiet by default

Nothing writes to the phone directly any more: everything goes through `alerts.raise_alert` with a
level (0 silent, 1 brief, 2 notification, 3 alarm, 4 call; 3 and 4 are Phase 3 and go out as
notifications for now). `notify.py` decides levels: a new task interrupts only when high/urgent or
due within 24 h; updates and completions are silent; unanswered questions and the 18:00 overdue
list are notifications; the morning brief is level 1. `QUIET_HOURS` (default 23:00-07:00, local)
holds everything below a call until it ends; the `alerts` job (every minute) sends held, snoozed
and failed ones. Delivery: the Orchestrator hub's `POST /api/notify` (`HUB_URL`, set in
`deploy/linux/native.env`), else Telegram with Done / Snooze / Not important buttons. The app
answers through `POST /alerts/{id}/ack`; done also finishes the task, dismiss is kept for
learning later. One open alert per `key`; `supersede` closes the previous brief or overdue list.

## Looking after itself

- Delivery order in `alerts.deliver`: hub → ntfy (`NTFY_TOPIC`, off when empty; level → ntfy
  priority 3/4/5; tap opens `NTFY_CLICK_URL`; acks stay in the app/Telegram) → Telegram.
  `alerts.channels()` lists what is set up. Tests mock ntfy with `alerts.ntfy_transport`.
- `health.py`, every 5 min: hub down > 30 min, nothing can reach you (logged loudly + failing
  dead-man ping), disk < 2 GB, db > 1 GB, backup failed/stale (`data/brain/backup-status`). One
  NOTIFY a day per check (`health:<name>:<date>`, even if dismissed); closed when it passes again.
  `HEALTHCHECK_URL` (healthchecks.io) is pinged each run. `GET /health/detail` (token) for the app.
  WhatsApp silence and Gmail errors live in `watcher.py`, not here.
- `deploy/linux/backup.sh`: restic to `BACKUP_REPO`, SQLite copied with `.backup` into
  `~/.cache/assistant-backup/sqlite` first (live `*.db` excluded), 7/4/6 retention, timer 03:45
  only when `BACKUP_REPO` is set. Restore: `deploy/linux/restore.md`.

## Models

One OpenAI-compatible client, two profiles in `.env`, six rotating Gemini keys shared by both:

- `CLASSIFIER_*` — `gemini-3.5-flash-lite`, every message. Keep the prompt tight; this is the bill.
- `PLANNER_*` — `gemini-3.8-flash`, brief and graph extraction. Falls back to the classifier model.

`llm.py` rotates keys on 429 (benching the key for 60 s) and retries transient 5xx with backoff
**without** benching — a busy model is not a bad key. `extract_json` salvages answers the model
truncated: it cuts at the last completed element and closes the brackets.

## Memory graph

`entity` / `edge` / `mention`. Fed by chats (free), the CV (one pass), and a nightly pass at 03:15.
`graph.context_for()` injects two or three lines into every classifier prompt — who the owner is
and who this sender is to him. That is what separates a programme he belongs to from a webinar
someone is selling him. Dedup is by slug of the name plus aliases; a bracketed suffix
("Indian Space Association (ISA)") is an alias, not a second entity. Never fuzzy-match: a wrong
merge is worse than a duplicate.

## Conventions

- Every behaviour change gets a test. Tests are offline: `conftest.py` injects a fake LLM
  (`LLM.override`), HTTP is mocked with `httpx.MockTransport`, Google services are faked.
  Live Google/Gemini calls cannot run in CI or in the cloud sandbox.
- Tests share one SQLite file, so a test that creates tasks must scope its assertions (filter by
  `source_chat`) or clean up after itself.
- New columns are added to the models and picked up automatically by `_migrate_columns()` on
  startup — SQLite `create_all` never alters existing tables.
- The bridge must stay read-only. Do not add a WhatsApp send path.
- Secrets live only in `.env` and `data/`. Both are git-ignored; keep it that way.
- Commit locally; the user pushes.

## Gotchas found the hard way

- Gemini returns `null` for string fields — the `Classification` model coerces them.
- Marketing mail imitates urgency ("Action Required", "you're invited", an amount). Bulk mail only
  passes the prefilter when it names money owed or *his own* booking/registration/account.
- Events whose time has already passed are never carded.
- WhatsApp LID ids (`…@lid`) must be resolved to phone-number jids or the same contact appears twice.
- `groupMetadata` per message hits WhatsApp's rate limit; fetch all group names once on connect.
- Telegram long-poll passes `timeout=25`, so the HTTP client needs a longer timeout than that.
- httpx's INFO log line contains the bot token — its logger is pinned to WARNING.
- gemini-3.x spends most of a small `max_tokens` budget on thinking tokens, so a 120-token cap
  returned half a sentence ("Roger Christopher is a Computer"). Summary calls get 600, and
  `clean_summary` rejects a short line with no full stop rather than storing the stump.
- The AI Studio usage dashboard is scoped to one Google Cloud project, so keys created in other
  projects look unused there. `/keys` in Telegram reports the real per-key tally.
- `ruff`'s default rule set drifts between releases; `[tool.ruff.lint] select` is pinned.

## Current state and next steps

WhatsApp, Gmail (2 accounts), Calendar and the graph are live. Next: tune on real traffic, then
the Omen worker (Whisper `/transcribe` + heartbeat — `perceive.py` already prefers a live `stt`
worker), then urgent phone calls (Twilio decision still open), then the Android app.

Build log and roadmap: https://claude.ai/code/artifact/d11e93a1-b748-4d23-8eb4-3c9e9c23791d
