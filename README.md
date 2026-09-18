# AI_Assistant

Personal Jarvis-style assistant. Reads WhatsApp (where the tasks arrive), extracts tasks and
events, keeps the schedule, and reaches the phone — push now, calls for urgent things later.

```
brain/    always-on orchestrator. CPU-only. Dev on the HP, production on the Mac mini.
bridge/   WhatsApp linked-device bridge (Baileys). Read-only. Forwards messages to the brain.
worker/   GPU / laptop-only jobs (Whisper, local model). Runs on the HP when it's on. Phase 4.
android/  the phone app. Phase 5. Telegram is the interim remote.
```

Rule that keeps the Mac mini migration trivial: **nothing in `brain/` or `docker-compose.yml`
may depend on a GPU or on the laptop.** Heavy stuff goes in `worker/`.

## Phase 0/1 — run it

1. `cp .env.example .env` and fill in:
   - `BRAIN_TOKEN` — `openssl rand -hex 24`
   - `OWNER_NAME` — your name as people write it in chats
   - `CLASSIFIER_API_KEY` — a Gemini key from AI Studio (free tier is fine for this)
   - `PLANNER_API_KEY` — OpenRouter (only used for the daily brief for now)
   - `TELEGRAM_BOT_TOKEN` — from @BotFather. Then send `/start` to your bot once the brain
     is up; it replies with your chat id, put that in `TELEGRAM_CHAT_ID`.
2. `docker compose up --build brain` — check `http://localhost:8000/health`.
3. In another terminal, `docker compose up bridge` (attached) and scan the QR from
   WhatsApp → Linked devices. The session persists in `data/bridge-auth/` — back it up.
4. Send yourself a WhatsApp message like "remind me to submit the form tomorrow 5pm".
   Within a few seconds Telegram shows the extracted task with Keep / Not-a-task buttons.
5. `docker compose up -d` for both once it works.

Without Docker (what the Mac mini will do):

```
cd brain && pip install . && uvicorn app.main:app --host 0.0.0.0 --port 8000
cd bridge && npm install && npm start
```

## API (what the Android app will use)

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

## Data & privacy

`data/` holds your WhatsApp session, the SQLite db with every message, and voice notes.
It is git-ignored. It lives on your own hardware behind Tailscale; keep it that way.
The bridge has no code path that sends WhatsApp messages.
