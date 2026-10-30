from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    brain_token: str = "change-me"
    owner_name: str = "the user"  # how the classifier refers to you, e.g. "Kishaunjith"
    owner_aliases: str = ""  # comma-separated nicknames / @number people use for you in chats
    # groups: only send a message to the model if it mentions you or contains a task/time signal
    group_keyword_gate: bool = True
    database_url: str = "sqlite:///./data/brain.db"
    media_dir: str = "./data/media"
    tz: str = "Asia/Kolkata"

    # cheap, high-volume model
    classifier_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    classifier_api_key: str = ""
    classifier_api_keys: str = ""  # comma-separated; rotated round-robin, overrides the single key
    classifier_model: str = "gemini-3.5-flash-lite"
    classifier_min_confidence: float = 0.6
    # tried when the classifier model fails (busy, all keys rate-limited); empty = none
    classifier_fallback_base_url: str = ""
    classifier_fallback_api_keys: str = ""
    classifier_fallback_model: str = ""

    # strong, low-volume model
    planner_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    planner_api_key: str = ""
    planner_api_keys: str = ""
    planner_model: str = "gemini-3.8-flash"
    # PLANNER_BASE_URL=hub + PLANNER_MODEL=codex thinks with your ChatGPT session through the
    # Orchestrator hub; the fallback (e.g. Gemini or Groq by API) is used when that fails.
    planner_fallback_base_url: str = ""
    planner_fallback_api_keys: str = ""
    planner_fallback_model: str = ""

    # perceive: voice notes -> transcript, images -> description (multimodal Gemini, same keys)
    perceive_enabled: bool = True
    perceive_model: str = ""  # defaults to classifier_model
    perceive_max_bytes: int = 8_000_000

    # google (calendar now; gmail/drive later). credentials.json + token.json live here
    google_dir: str = "./data/google"
    calendar_name: str = "Assistant"
    calendar_sync_tasks_with_due: bool = True  # tasks with a due time also get a 30-min block
    gmail_poll_minutes: int = 5
    gmail_backfill_days: int = 3  # first poll looks back this far

    daily_brief_hour: int = 8

    # questions to you: surface only if unanswered after this many minutes (0 = surface at once)
    question_grace_minutes: int = 20

    # alerts (alerts.py): quiet by default, delivered through the Orchestrator hub's phone app
    hub_url: str = ""  # e.g. http://127.0.0.1:4317; empty = ntfy only (if set)
    orc_token: str = ""  # the hub's ORC_TOKEN (the hub also trusts plain loopback calls)
    quiet_hours: str = "23:00-07:00"  # local time; only calls (level 4) ring inside it. Empty = none
    snooze_minutes: int = 60
    # ntfy (https://ntfy.sh or self-hosted): fallback when the hub does not answer. Off while the
    # topic is empty. The topic is the only secret on ntfy.sh: make it long and random.
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = ""
    ntfy_token: str = ""  # optional access token (self-hosted server with auth, or ntfy.sh account)
    ntfy_click_url: str = ""  # opened when the notification is tapped, e.g. http://<hub tailscale ip>:4317

    # health (health.py): self-checks every 5 minutes and a dead-man ping
    healthcheck_url: str = ""  # healthchecks.io ping URL; empty = no dead-man ping
    health_hub_down_minutes: int = 30  # alert when the hub has not answered for this long
    health_min_free_gb: float = 2.0  # alert when the data volume has less free space than this
    health_max_db_mb: int = 1024  # alert when brain.db (plus its -wal) grows past this
    # nightly restic backup (deploy/linux/backup.sh); set here so the health check watches it
    backup_repo: str = ""
    health_backup_max_hours: int = 36  # alert when the last good backup is older than this

    # the watcher (watcher.py): deadlines at risk, broken promises, bills, renewals, health, calls
    watch_enabled: bool = True
    watch_minutes: int = 5
    watch_whatsapp_silent_hours: int = 12  # no WhatsApp message this long (while it normally flows)
    escalate_minutes: int = 5  # an unanswered alarm becomes a call after this long
    calls_per_day: int = 2
    watch_judge: bool = True  # ask the classifier model "worth interrupting now?" (8 s, rule wins on error)
    watch_vip_chats: str = ""  # comma-separated chat/sender ids whose unanswered questions ring (alarm)

    # background work (review.py)
    review_enabled: bool = True  # weekly review (Sunday 18:00) and monthly noise report (1st, 09:00)
    drafts_enabled: bool = False  # Gmail draft replies to personal questions; never sent

    # how many prior messages from the same chat to show the classifier
    context_window: int = 6
    # extra open tasks from other chats/senders offered for update/complete (confirmations often
    # arrive from a different address than the request)
    cross_chat_task_candidates: int = 12
    cross_chat_task_days: int = 14

    # memory graph
    graph_enabled: bool = True
    graph_daily_messages: int = 300   # most messages one nightly pass will read
    graph_daily_chunks: int = 6       # planner calls per nightly pass (cost ceiling)
    graph_hour: int = 3               # local hour the nightly pass runs
    graph_max_tokens: int = 8000      # a CV with 15+ entities needs room; truncation loses relations

    # shared memory (memory.py): graph entities/edges and tasks copied in this often; 0 = off
    memory_mirror_minutes: int = 2

    # the thinking loop (agent.py): POST /agent/ask, from the app through the hub
    agent_enabled: bool = True
    agent_max_steps: int = 8       # tool calls per request
    # Spoken requests think with a fast model: "classifier" (CLASSIFIER_* keys, e.g. Gemini Flash-Lite, ~1 s a
    # step) with the planner as its fallback, "planner" for the planner (through the hub each step runs a
    # whole Codex/Claude session, 10-25 s), or a model name on the planner's endpoint.
    agent_voice_model: str = "classifier"
    agent_max_tokens: int = 2000   # per model turn; gemini-3.x spends part of it thinking
    # what each device name is, in your words, so "the laptop" finds the right one, e.g.
    # "roger = the HP Omen laptop (Windows, RTX 5050); linux-hub = the always-on Linux machine; mac-mini = the Mac mini"
    agent_device_notes: str = ""

    # the learning harness (learn.py, docs/LEARNING.md): episodes, feedback, lessons, nightly reflection + evaluation gate
    learn_enabled: bool = True
    learn_hour: int = 3                    # nightly pass (local time), after the graph's
    learn_trial_confidence: float = 0.7    # reflected preferences/facts at or above go on trial; the rest wait for you
    learn_off_after_losses: int = 2        # a lesson thumbed down this often (and more than up) turns itself off
    learn_eval_enabled: bool = True        # run the evaluation gate when lessons are on trial (costs model calls)
    learn_eval_builtin: bool = True        # include agent_eval's built-in cases, not only the learnt ones
    learn_eval_model: str = ""             # e.g. codex; empty = PLANNER_MODEL
    learn_eval_tolerance: float = 0.05     # pass-rate drop allowed before trial lessons are rolled back
    learn_eval_timeout_minutes: int = 40


settings = Settings()
