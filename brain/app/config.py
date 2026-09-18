from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    brain_token: str = "change-me"
    owner_name: str = "the user"  # how the classifier refers to you, e.g. "Kishaunjith"
    database_url: str = "sqlite:///./data/brain.db"
    media_dir: str = "./data/media"
    tz: str = "Asia/Kolkata"

    # cheap, high-volume model
    classifier_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    classifier_api_key: str = ""
    classifier_model: str = "gemini-2.5-flash"
    classifier_min_confidence: float = 0.6

    # strong, low-volume model
    planner_base_url: str = "https://openrouter.ai/api/v1"
    planner_api_key: str = ""
    planner_model: str = "anthropic/claude-sonnet-4"

    # telegram interim remote
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    daily_brief_hour: int = 8

    # how many prior messages from the same chat to show the classifier
    context_window: int = 6


settings = Settings()
