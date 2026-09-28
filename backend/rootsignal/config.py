from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App settings. Values come from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: Literal["mock", "gemini", "grok"] = "mock"
    gemini_api_key: str = ""
    grok_api_key: str = ""

    database_url: str = "postgresql+asyncpg://rootsignal:rootsignal@localhost:5432/rootsignal"

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"

    cors_origins: str = "http://localhost:5173"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
