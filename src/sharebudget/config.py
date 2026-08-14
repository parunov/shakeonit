from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str
    database_path: Path = Path("data/sharebudget.db")
    log_level: str = "INFO"
    webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_path: str = "/telegram/webhook"
    webhook_host: str = "127.0.0.1"
    webhook_port: int = 8080
    webhook_cert_path: Path | None = None
    webhook_key_path: Path | None = None
    webapp_url: str | None = None
    analytics_url: str | None = None
    # Telegram can resume an existing WebView instead of issuing fresh initData.
    # A seven-day bootstrap window bridges upgrades for returning users; after the
    # first successful request an HttpOnly rolling session is used instead.
    webapp_auth_max_age: int = 7 * 24 * 60 * 60
    bot_username: str = "ShakeOnIt_bot"
    main_app_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
