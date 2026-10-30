# AI_Assistant — working notes for Claude

Personal Jarvis-style assistant for **Roger Christopher**. Reads WhatsApp and Gmail, turns real
commitments into tracked tasks and calendar events, and reaches the phone. Owner name and aliases
live in `.env` (`OWNER_NAME`, `OWNER_ALIASES`).

## Layout

```
brain/    always-on orchestrator. FastAPI + SQLModel(SQLite) + APScheduler. CPU-only, no GPU deps.
bridge/   WhatsApp linked-device bridge (Baileys, Node 20). READ-ONLY: no send path exists.
worker/   GPU / laptop-only jobs (Whisper, local model). Heartbeat stub only so far — Phase 4.
android/  unused: the phone app is the Orchestrator app (../Orchestrator/mobile), through its hub.
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
cd brain && uv run --extra dev pytest -q    # 137 tests, all offline (fake LLM + mocked HTTP)
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
`deploy/linux/native.env`), else ntfy. The app
answers through `POST /alerts/{id}/ack`; done also finishes the task, dismiss is kept for
learning later. One open alert per `key`; `supersede` closes the previous brief or overdue list.

## The watcher and background work

`watcher.py` runs every 5 minutes; every rule has its own alert key and a key that was ever raised
(answered or not) is never raised again, so "done" or "not important" does not come back. Rules:
deadline within 6 h → notify (`risk6:`), 1 h → alarm (`risk1:`; at night an accepted task calls
straight away, since alarms are held through quiet hours); a promise of yours past due → notify
(found from the task's source message or a `from_me` message whose stored classification updated
it); bills 2 days before → notify, due day → alarm; renewals 7 days before and on the day (tasks,
and mails that name a renewal/expiry word plus a date with a year); WhatsApp silent 12 h while it
normally flows, Gmail failing (Note `gmail_error_<label>`, written by `gmail.record_poll`).
An alarm unanswered for `ESCALATE_MINUTES` (sent in the last 2 h) becomes a call, `CALLS_PER_DAY`
max; in quiet hours only deadline/bill alarms call. All watcher alerts go through
`watcher.interrupt()`: learning (3 dismissals of a kind from a chat in 30 days → one level
quieter; notify.py's new_task and question alerts use it too), then the cheap model's yes/no
(`WATCH_JUDGE`; "no" drops a level, notification → silent, recorded as a level-0 `done` row).
Who is waiting: `close_relation()` matches the sender to a graph person by exact key or alias only,
then a close relation edge or a family/professor/manager word in the summary → alarm.
`review.py`: weekly review (Sun 18:00), monthly noise report (1st 09:00), Gmail drafts
(`DRAFTS_ENABLED`, off; never sent). Tests run the watcher at a time a month ahead so rows other
tests wrote "now" are out of its windows.

## Looking after itself

- Delivery order in `alerts.deliver`: hub → ntfy (`NTFY_TOPIC`, off when empty; level → ntfy
  priority 3/4/5; tap opens `NTFY_CLICK_URL`; acks stay in the app).
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

## Shared memory

`memory.py` + `routers/memory.py`, plan in `docs/JARVIS.md`. One table, `memory_record`, is the
master copy of the memory every device shares; the Orchestrator keeps a replica per device and
syncs through `/memory/changes?since=<seq>` (pull) and `/memory/batch` (push).

- Every accepted write takes the next `seq` (unique). Unchanged content is not rewritten
  (`fingerprint`), so the mirror can run every 2 minutes without filling the log.
- Same uid from two places: newer `updated_at` wins, a tie keeps ours. A refused write is answered
  with our copy (`record`) so the device converges. Deletes are tombstones.
- `uid` is `<kind>:<slug>` for named things (the same on every device) and `<kind>:<random>` else.
- `mirror()` copies graph entities/edges (`person:jayesh-daga`, `relation:a~rel~b`) and tasks
  (`task:<id>`) in, and tombstones mirrored records whose row is gone (merged entities).
  These are owned by the brain; other knowledge about them is a `fact` with `subject`.
- Search is FTS5 (`memory_fts`, created in `init_fts`, not by `create_all`): all words, then any.
  Name lookups (`resolve`) never use the any-word fallback; it matched made-up names.
- `python -m app.memory_import notes/*.md` (or `POST /memory/import`) loads one markdown file per
  project; bullets under a "Decisions" heading become `decision` records about it.

## No Telegram

Telegram was removed on 29 Oct 2026 (the owner's call: everything goes through the Orchestrator
app). What it did now lives in the app: alerts (Home → Needs you), tasks to confirm (Tasks: Keep /
Not a task / Done), the brief on demand, memory (/who, /graph → Memory), WhatsApp chat muting,
model key usage, and free-text chat (Assistant tab). The brain side is `routers/control.py` plus
`/tasks` and `/agent/ask`; the hub passes an allow-list of paths through `/api/brain/*`.
Old TELEGRAM_* lines in a `.env` are ignored. Don't add another chat-app transport without asking.

## The thinking loop

`agent.py` (`POST /agent/ask`; the app's Assistant tab reaches it through the hub's `/api/assistant/ask`).
Recall (memory search for the request, into the prompt) → up to `AGENT_MAX_STEPS` JSON moves on
the planner model, each a tool call or `{"answer"}` → a `work` record of what was asked and done.

- Tools: `memory_search`, `memory_about`, `remember`, `tasks`, `add_task`, `act`, `build` (hub `POST /api/builds`: an app made or continued on its own branch; reported as STARTED, the owner checks the preview and merges in the app), `phone` (hub `POST /api/phone/actions`: an alarm, reminder, app to open, file, or a call/text to open filled in; reported as WAITING until he taps Do it on the phone) (the hub's
  `/api/act`, so any device's actions). There is deliberately no tool that approves anything:
  risky actions come back `pending` and the loop must say they have NOT run.
- JSON moves, not provider tool-calling: works on every OpenAI-compatible model with the same key
  rotation, and the tests script it (`tests/test_agent.py`, `Hub.transport` for the hub).
- Malformed replies and the step cap get 3 extra turns, so the model can still answer (a test
  caught the cap eating the answer turn).

## Tuning the thinking loop

`python -m app.agent_eval` (on Linux: `deploy/linux/run brain python -m app.agent_eval`) runs 17
realistic requests through the real loop on the planner model, each with its own seeded memory and
tasks in a throwaway database and a FAKE hub (canned devices and action results; nothing runs on a
device). It checks the actions and tools used, what must not happen (delete, messaging, writes when
asked to read, acting on an offline device), approvals left pending, the answer text and, for
tasks/remember, the database. `--model`, `--repeat`, `--only`, `--out report.json`.
Tune the prompt (`SYSTEM` in agent.py), rerun, compare models. Add a case for every real failure.
The cloud sandbox cannot reach Gemini or Groq (egress allowlist): run it on the laptop or Linux.

What the loop tolerates (all tested): prose or fences around the JSON, two objects in one reply
(first wins), tool-name aliases (`search_memory`, `create_task`…), flattened `act` calls, an
`{"tool": "answer"}` move, the same call twice (not rerun; the model is told to use the result).
Follow-ups: `/agent/ask` takes `history` (the app sends its last three exchanges); recall searches
the previous question too. `AGENT_DEVICE_NOTES` says what each device name is ("roger = the HP
Omen laptop…") so "the laptop" resolves.

- Thinking with ChatGPT instead of Gemini: `PLANNER_BASE_URL=hub` + `PLANNER_MODEL=codex` sends the planner's calls (thinking loop, brief, reviews) to the hub's `/v1/chat/completions`, which runs `codex exec` logged in with the owner's ChatGPT account (`claude` and `groq` work the same way; the brain sends them as `raw:codex` etc., a plain call with no Orchestrator run or phone alert). `PLANNER_MODEL=auto` sends `raw:auto`: the hub's model router (its `src/ai/route.ts`, job `brain`: ChatGPT, Claude, Gemini, Groq, skipping cooling ones) picks the session and falls back on failure. Calls get at least 240 s. `PLANNER_FALLBACK_*` (and `CLASSIFIER_FALLBACK_*`) is tried when the primary fails. Eval: `python -m app.agent_eval --model codex` (uses the real hub at `--hub`, default http://127.0.0.1:4317; the devices stay fake).

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
- httpx's INFO log line contains the bot token — its logger is pinned to WARNING.
- gemini-3.x spends most of a small `max_tokens` budget on thinking tokens, so a 120-token cap
  returned half a sentence ("Roger Christopher is a Computer"). Summary calls get 600, and
  `clean_summary` rejects a short line with no full stop rather than storing the stump.
- The AI Studio usage dashboard is scoped to one Google Cloud project, so keys created in other
  projects look unused there. Settings → Model key usage in the app (`GET /llm/usage`) reports the real per-key tally.
- `ruff`'s default rule set drifts between releases; `[tool.ruff.lint] select` is pinned.

## Current state and next steps

WhatsApp, Gmail (2 accounts), Calendar and the graph are live. Next: tune on real traffic, then
the Omen worker (Whisper `/transcribe` + heartbeat — `perceive.py` already prefers a live `stt`
worker), then urgent phone calls (Twilio decision still open), then the Android app.

Build log and roadmap: https://claude.ai/code/artifact/d11e93a1-b748-4d23-8eb4-3c9e9c23791d

## The learning harness (learn.py, docs/LEARNING.md)

- Every `agent.ask` is an `Episode`; the reply carries `episode_id` and the `lessons` that were in its prompt. The app's 👍/👎 posts `/learn/episodes/{id}/feedback`; a correction ("no, I meant…") within 30 minutes marks the previous episode bad; "from now on…" (`learn.TEACH`) becomes an active lesson at once.
- Lessons (preference, fact, skill, rule, voice) go into the prompt by `when` words (`learn.relevant`, at most 8; voice only for spoken requests), after the rules, with "the rules win".
- Nightly (`LEARN_HOUR`:40): `settle` (2+ 👎 and more 👎 than 👍 -> off), `reflect` (model -> lessons + regression cases from bad episodes), and when something is on trial, `evaluate`: `agent_eval --learned` in a subprocess with the lessons and learnt cases; pass rate below the last good run minus `LEARN_EVAL_TOLERANCE` rolls trial lessons back, else promotes them.
- `learn.UNSAFE` keeps anything about approvals, passwords, tokens, deleting, payments or ignoring rules out of lessons at every entry point. Keep it that way.
