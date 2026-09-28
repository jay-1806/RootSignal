"""Domain models shared by the engine, the API, and the providers."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from rootsignal.core.state_machine import InvestigationState


def utcnow() -> datetime:
    return datetime.now(UTC)


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Alert(BaseModel):
    """A normalized alert, whatever its source (Alertmanager, CLI, MCP, dashboard)."""

    title: str = Field(min_length=1, max_length=300)
    service: str = Field(min_length=1, max_length=100)
    severity: Severity = Severity.HIGH
    source: str = "manual"
    description: str = ""
    labels: dict[str, str] = Field(default_factory=dict)
    fired_at: datetime = Field(default_factory=utcnow)


class EvidenceKind(StrEnum):
    METRIC = "metric"
    LOG = "log"
    CHANGE = "change"
    PAST_INCIDENT = "past_incident"


class Evidence(BaseModel):
    kind: EvidenceKind
    summary: str
    query: str = ""  # the exact query that produced it, for audit
    supports: bool  # True = supports the hypothesis, False = contradicts
    weight: float = Field(ge=0.0, le=1.0)  # how strong the signal is
    data: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    id: str
    statement: str  # e.g. "DB connection pool exhausted in checkout"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # computed, not LLM-reported
    evidence: list[Evidence] = Field(default_factory=list)


class StateChange(BaseModel):
    from_state: InvestigationState | None
    to_state: InvestigationState
    at: datetime = Field(default_factory=utcnow)
    note: str = ""
