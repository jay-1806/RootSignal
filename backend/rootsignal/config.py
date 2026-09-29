from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def clean_secret(value: str) -> str:
    """Undo common copy-paste mistakes: surrounding quotes, whitespace or a Windows
    line ending (\\r), and a pasted 'Bearer ' prefix."""
    value = (value or "").strip().strip("\"'").strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    return value


class Settings(BaseSettings):
    """App settings. Values come from environment variables or a .env file."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- LLM. "mock" = no LLM calls: the rule-based reasoner does all the reasoning.
    llm_provider: Literal["mock", "gemini", "grok"] = "mock"
    llm_fallback: bool = True  # if the primary LLM fails, try the other one (if it has a key)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash"
    grok_api_key: str = ""
    grok_model: str = "grok-4.7"
    llm_timeout_seconds: float = 60.0
    # USD per 1M tokens, for cost tracking only. Check your provider's pricing page.
    gemini_price_input: float = 0.0
    gemini_price_output: float = 0.0
    grok_price_input: float = 2.0
    grok_price_output: float = 6.0

    # --- Storage
    database_url: str = "postgresql+asyncpg://rootsignal:rootsignal@localhost:5432/rootsignal"
    auto_migrate: bool = True  # run Alembic migrations on startup (Postgres only)

    # --- Data sources (all read-only)
    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"
    changes_url: str = "http://localhost:8090"  # fault-control's change log in the demo

    # --- Investigation engine
    autostart: bool = True  # start investigating as soon as an investigation is created
    lookback_minutes: int = 15  # telemetry window before "now"
    changes_lookback_minutes: int = 60  # how far back to look for deploys/config changes
    max_iterations: int = 2  # hypothesis -> evidence rounds before concluding
    conclude_confidence: float = 0.7  # stop refining once the top hypothesis reaches this

    # --- API
    cors_origins: str = "http://localhost:5173"
    alert_webhook_token: str = ""  # if set, webhooks must send "Authorization: Bearer <token>"

    @field_validator("gemini_api_key", "grok_api_key", "alert_webhook_token", mode="before")
    @classmethod
    def _clean_secret(cls, value: str) -> str:
        return clean_secret(value)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
