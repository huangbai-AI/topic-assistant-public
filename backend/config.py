from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    database_url: str = "sqlite:///data/topic_assistant.db"
    data_dir: Path = Path("data")

    feishu_webhook_url: str = ""
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_chat_id: str = ""
    feishu_event_signing_secret: str = ""
    public_base_url: str = ""
    risk_artifact_token_secret: str = ""
    risk_artifact_token_ttl_seconds: int = 600
    risk_artifact_legacy_api_key_days: int = 7

    api_key: str = "changeme"

    scrape_headless: bool = True
    scrape_delay_min: float = 2.0
    scrape_delay_max: float = 5.0
    scrape_max_results: int = 20
    scrape_retry_attempts: int = 2
    scrape_keyword_timeout_seconds: int = 120
    cookie_dir: Path = Path("data/cookies")
    xhs_user_data_dir: Path = Path("data/browser_profiles/xiaohongshu")
    xhs_cookies_json: str = ""
    xhs_cookies_base64: str = ""
    xhs_cookies_overwrite: bool = False
    risk_alert_cooldown_seconds: int = 300
    xhs_login_recover_wait_seconds: int = 90

    schedule_enabled: bool = True
    schedule_cron_hour: int = 9
    schedule_cron_minute: int = 0
    cors_allow_origins: str = (
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:4173,"
        "http://127.0.0.1:4173,"
        "http://localhost:5500,"
        "http://127.0.0.1:5500"
    )

    model_config = {"env_prefix": "TA_", "env_file": ".env"}


settings = Settings()

settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.cookie_dir.mkdir(parents=True, exist_ok=True)
settings.xhs_user_data_dir.mkdir(parents=True, exist_ok=True)
