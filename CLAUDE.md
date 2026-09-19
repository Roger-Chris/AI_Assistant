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

**The rule that keeps the Mac mini migration trivial: nothing in `brain/`, `bridge/` or
`docker-compose.yml` may depend on a GPU or on this laptop.** Heavy work goes in `worker/` and
talks to the brain over HTTP. The brain currently runs on the HP Omen for development; the Mac
mini becomes the permanent host once it is reset.

## Commands

```bash
docker compose up --build -d brain bridge   # rebuild + restart
docker compose logs -f brain                # follow
cd brain && uv run --extra dev pytest -q    # 41 tests, all offline (fake LLM + mocked HTTP)
cd brain && ruff check app tests
docker compose run --rm -p 8765:8765 brain python -m app.google_auth [label]   # OAuth per account
docker compose run --rm brain python -m app.seed_profile /data/profile/resume.docx
```

Read the database while the container runs with **read-only** sqlite
(`sqlite3.connect('file:data/brain/brain.db?mode=ro', uri=True)`). Writing to it from outside the
container gives "disk I/O error".

## How a message flows

`ingest` → muted-chat check → `perceive` (voice notes transcribed, images described) →
`prefilter` (rule-based, free) → `classifier` (cheap model) → notify.

The classifier is **task-aware**: it sees the chat's open tasks plus recent open tasks from the
same source, and returns an action — `new` / `update` / `complete` / `none`. That is what stops
"can you send X" + "I'll send in 10 min" becoming two cards, and lets a confirmation mail close a
registration task even from a different sender.

Questions to the owner become `watching` tasks: silent unless unanswered past
`QUESTION_GRACE_MINUTES`, and closed silently by any reply from him in that chat.

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

## Current state and next steps

WhatsApp, Gmail (2 accounts), Calendar and the graph are live. Next: tune on real traffic, then
the Omen worker (Whisper `/transcribe` + heartbeat — `perceive.py` already prefers a live `stt`
worker), then urgent phone calls (Twilio decision still open), then the Android app.

Build log and roadmap: https://claude.ai/code/artifact/d11e93a1-b748-4d23-8eb4-3c9e9c23791d
