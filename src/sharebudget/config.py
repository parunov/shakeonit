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

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
