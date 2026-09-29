"""Domain models shared by the engine, the API, and the providers."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from rootsignal.core.state_machine import InvestigationState
from rootsignal.providers.remediation import RemediationAction


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


class Category(StrEnum):
    """Kinds of root cause. Deliberately broader than the demo's fault scenarios."""

    DEPLOY_REGRESSION = "deploy_regression"
    CONFIG_CHANGE = "config_change"
    DB_CONNECTION_EXHAUSTION = "db_connection_exhaustion"
    CACHE_DEGRADATION = "cache_degradation"
    MEMORY_LEAK = "memory_leak"
    DOWNSTREAM_FAILURE = "downstream_failure"
    TRAFFIC_SURGE = "traffic_surge"
    INFRASTRUCTURE = "infrastructure"
    OTHER = "other"


class EvidenceKind(StrEnum):
    METRIC = "metric"
    LOG = "log"
    CHANGE = "change"
    PAST_INCIDENT = "past_incident"


class Evidence(BaseModel):
    kind: EvidenceKind
    summary: str
    check: str = ""  # which catalog check produced it
    service: str = ""
    query: str = ""  # the exact query that produced it, for audit
    supports: bool  # True = supports the hypothesis, False = contradicts
    weight: float = Field(ge=0.0, le=1.0)  # how strong the signal is
    started_at: datetime | None = None  # when the anomaly began, if known
    data: dict[str, Any] = Field(default_factory=dict)


class Hypothesis(BaseModel):
    id: str
    statement: str  # e.g. "DB connection pool exhausted in inventory"
    category: Category = Category.OTHER
    service: str = ""
    rationale: str = ""
    source: str = ""  # "rule_based" or the LLM provider that proposed it
    iteration: int = 1
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)  # computed, not LLM-reported
    evidence: list[Evidence] = Field(default_factory=list)


class AlternativeHypothesis(BaseModel):
    statement: str
    confidence: float


class RCA(BaseModel):
    """Final root-cause analysis. Facts and confidence come from code; the narrative
    (`summary`, `reasoning`) comes from the reasoner (LLM or template)."""

    hypothesis_id: str
    root_cause: str
    category: Category
    service: str
    confidence: float
    conclusive: bool  # confidence reached the configured threshold
    summary: str
    reasoning: str = ""
    started_at: datetime | None = None
    affected_services: list[str] = Field(default_factory=list)
    triggering_change: str | None = None
    evidence: list[Evidence] = Field(default_factory=list)  # strongest supporting evidence
    alternatives: list[AlternativeHypothesis] = Field(default_factory=list)
    recommended_action: RemediationAction | None = None
    written_by: str = ""  # "rule_based" or LLM provider name


class StateChange(BaseModel):
    from_state: InvestigationState | None
    to_state: InvestigationState
    at: datetime = Field(default_factory=utcnow)
    note: str = ""
