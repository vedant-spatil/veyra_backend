from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(BACKEND_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+psycopg://veyra:veyra@127.0.0.1:5432/veyra"
    redis_url: str = "redis://127.0.0.1:6379/0"
    secret_key: str = "dev-only-change-me"
    public_base_url: str = "http://127.0.0.1:8787"
    frontend_origins: str = "http://127.0.0.1:5173"
    allowed_emails: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    dograh_base_url: str = "http://127.0.0.1:8000"
    dograh_api_key: str = ""
    payu_env: str = "test"
    payu_key: str = ""
    payu_salt: str = ""
    calcom_api_key: str = ""
    cookie_secure: bool = False

    @property
    def cors_origins(self) -> list[str]:
        return [item.strip() for item in self.frontend_origins.split(",") if item.strip()]

    @property
    def allowed_email_set(self) -> set[str]:
        return {item.strip().lower() for item in self.allowed_emails.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
