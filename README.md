# AI_Assistant

Personal Jarvis-style assistant. Reads WhatsApp (where the tasks arrive), extracts tasks and
events, keeps the schedule, and reaches the phone — push now, calls for urgent things later.

```
brain/    always-on orchestrator. CPU-only. Production on the Linux machine, dev on the HP.
bridge/   WhatsApp linked-device bridge (Baileys). Read-only. Forwards messages to the brain.
worker/   GPU / laptop-only jobs (Whisper, local model). Runs on the HP when it's on. Phase 4.
android/  not used: the phone app is the Orchestrator app (../Orchestrator/mobile), through its hub.
```

Rule that keeps moving hosts trivial: **nothing in `brain/`, `bridge/` or `docker-compose.yml`
may depend on a GPU or on the laptop.** Heavy stuff goes in `worker/`.

**On the Linux machine** the brain and bridge run natively as systemd services, no Docker:
see [deploy/linux/README.md](deploy/linux/README.md) (`deploy/linux/install.sh`). The Docker
steps below are for running it on the Windows laptop.

## Phase 0/1 — run it

1. `cp .env.example .env` and fill in:
   - `BRAIN_TOKEN` — `openssl rand -hex 24`
   - `OWNER_NAME` — your name as people write it in chats
   - `CLASSIFIER_API_KEY` — a Gemini key from AI Studio (free tier is fine for this)
   - `PLANNER_API_KEY` — OpenRouter (only used for the daily brief for now)
   - `HUB_URL` / `ORC_TOKEN` — the Orchestrator hub, which is how the phone app reaches the brain.
2. `docker compose up --build brain` — check `http://localhost:8000/health`.
3. In another terminal, `docker compose up bridge` (attached) and scan the QR from
   WhatsApp → Linked devices. The session persists in `data/bridge-auth/` — back it up.
4. Send yourself a WhatsApp message like "remind me to submit the form tomorrow 5pm".
   Within a few seconds the app shows the extracted task under Tasks with Keep / Not a task.
5. `docker compose up -d` for both once it works.

Without Docker, on Linux: `deploy/linux/install.sh` (see [deploy/linux/README.md](deploy/linux/README.md)).

## API (what the phone app uses, through the hub's /api/brain/*)

All routes need `Authorization: Bearer $BRAIN_TOKEN`.

| route | what |
|---|---|
| `GET /health` | liveness (no auth) |
| `POST /ingest/whatsapp` | bridge → brain, one message |
| `GET /tasks?status=` | open + unconfirmed by default |
| `POST /tasks` | create by hand |
| `POST /tasks/{id}/confirm` · `/reject` · `/done` | state changes |
| `PATCH /tasks/{id}` | edit title / due / priority |
| `POST /workers/heartbeat` · `GET /workers` | worker liveness |
| `POST /debug/brief` | send the morning brief now |

## How classification works

Every inbound message → cheap model (`CLASSIFIER_*`) with the last 6 messages of that chat
as context → JSON `{kind, title, due_at, people, priority, confidence}`. `task` / `event` /
`question` above `CLASSIFIER_MIN_CONFIDENCE` become a `pending_confirmation` task and get
pushed to you. Everything else is stored, tagged and ignored. Empty / media-only messages
never hit the model. The raw verdict is kept on the `message` row so you can audit misfires.

Tune the prompt in `brain/app/classifier.py`; run `cd brain && pytest` after.

## History (context)

With `HISTORY_SYNC=recent` (default) or `full`, the bridge forwards the chat history WhatsApp
pushes at link time to `POST /ingest/whatsapp/history`. Those rows are stored `classified=true`:
they are never triaged (no quota, no flood of stale task cards) but they are the context the
classifier sees for new messages and the raw material for the memory graph later. History only
arrives when a device links, so to (re)enable it: unlink the bridge in WhatsApp → Linked devices,
delete `data/bridge-auth/`, restart the bridge and re-scan.

## Memory graph

People, organisations and projects, with the relations between them, in three SQLite tables
(`entity`, `edge`, `mention`). Filled from three places: every chat and email sender becomes a
node for free; one pass over your CV seeds you, your college and your projects; a nightly planner
pass reads the day's kept messages. `graph.context_for()` puts two or three lines into every
classifier prompt — who you are, who this sender is to you — which is what separates a programme
you belong to from a webinar someone is selling you.

Seed it: put your CV at `data/brain/profile/resume.docx` and run
`deploy/linux/run brain python -m app.seed_profile ../data/brain/profile/resume.docx` on Linux
(with Docker: `docker compose run --rm brain python -m app.seed_profile /data/profile/resume.docx`).
Inspect it in the app: Settings → Memory.

## Data & privacy

`data/` holds your WhatsApp session, the SQLite db with every message, and voice notes.
It is git-ignored. It lives on your own hardware behind Tailscale; keep it that way.
The bridge has no code path that sends WhatsApp messages.
