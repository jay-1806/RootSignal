"""Change provider interface: "what changed before it broke?" (deploys, config, commits)."""

from abc import ABC, abstractmethod
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel


class ChangeKind(StrEnum):
    DEPLOY = "deploy"
    CONFIG = "config"
    COMMIT = "commit"
    FEATURE_FLAG = "feature_flag"


class Change(BaseModel):
    kind: ChangeKind
    service: str
    at: datetime
    summary: str  # e.g. "checkout v1.8.0 -> v1.9.0"
    ref: str = ""  # commit SHA, image tag, or URL


class ChangeProvider(ABC):
    name: str

    @abstractmethod
    async def recent_changes(self, service: str | None, since: datetime) -> list[Change]:
        """Changes since `since`, optionally filtered to one service."""
