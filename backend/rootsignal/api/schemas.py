from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from rootsignal.core.models import Hypothesis, StateChange
from rootsignal.core.state_machine import InvestigationState


class InvestigationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    service: str
    severity: str
    source: str
    state: InvestigationState
    alert: dict[str, Any]
    history: list[StateChange]
    hypotheses: list[Hypothesis]
    rca: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class HealthOut(BaseModel):
    status: str
    version: str
    llm_provider: str
    database: str
