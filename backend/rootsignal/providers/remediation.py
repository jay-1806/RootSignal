"""Remediation interface.

Safety by construction: the only runnable actions are the ones in `ActionType`, each
with typed parameters. There is no "run this shell command" action, and the engine
only calls `execute` after a human approves.
"""

from abc import ABC, abstractmethod
from enum import StrEnum

from pydantic import BaseModel, Field


class ActionType(StrEnum):
    RESTART_SERVICE = "restart_service"
    ROLLBACK_DEPLOY = "rollback_deploy"
    TOGGLE_FEATURE_FLAG = "toggle_feature_flag"


class RemediationAction(BaseModel):
    type: ActionType
    service: str = Field(min_length=1, max_length=100)
    params: dict[str, str] = Field(default_factory=dict)  # e.g. {"to_version": "v1.8.0"}
    reason: str = ""


class RemediationResult(BaseModel):
    success: bool
    message: str


class Remediator(ABC):
    name: str

    @abstractmethod
    async def execute(self, action: RemediationAction) -> RemediationResult:
        """Run an approved action. Implementations must reject services they don't manage."""
