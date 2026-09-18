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

    # strong, low-volume model
    planner_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    planner_api_key: str = ""
    planner_api_keys: str = ""
    planner_model: str = "gemini-3.8-flash"

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

    # telegram interim remote
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    daily_brief_hour: int = 8

    # questions to you: surface only if unanswered after this many minutes (0 = surface at once)
    question_grace_minutes: int = 20

    # how many prior messages from the same chat to show the classifier
    context_window: int = 6


settings = Settings()
