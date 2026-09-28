from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from rootsignal.core.models import RCA, Category, Hypothesis, StateChange
from rootsignal.core.state_machine import InvestigationState
from rootsignal.db.models import InvestigationRow


class LLMCallOut(BaseModel):
    purpose: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float
    success: bool
    error: str | None
    created_at: datetime


class LLMUsageSummary(BaseModel):
    calls: int = 0
    failed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class InvestigationOut(BaseModel):
    id: str
    title: str
    service: str
    severity: str
    source: str
    state: InvestigationState
    alert: dict[str, Any]
    history: list[StateChange]
    hypotheses: list[Hypothesis]
    rca: RCA | None
    context: dict[str, Any] | None
    iterations: int
    reasoner: str
    llm_usage: LLMUsageSummary
    llm_calls: list[LLMCallOut]
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: InvestigationRow) -> "InvestigationOut":
        calls = [LLMCallOut.model_validate(c, from_attributes=True) for c in row.llm_calls]
        usage = LLMUsageSummary(
            calls=len(calls),
            failed_calls=sum(not c.success for c in calls),
            input_tokens=sum(c.input_tokens for c in calls),
            output_tokens=sum(c.output_tokens for c in calls),
            cost_usd=round(sum(c.cost_usd for c in calls), 6),
            latency_ms=round(sum(c.latency_ms for c in calls), 1),
        )
        return cls(
            id=row.id,
            title=row.title,
            service=row.service,
            severity=row.severity,
            source=row.source,
            state=InvestigationState(row.state),
            alert=row.alert,
            history=row.history,
            hypotheses=row.hypotheses or [],
            rca=row.rca,
            context=row.context,
            iterations=row.iterations or 0,
            reasoner=row.reasoner or "",
            llm_usage=usage,
            llm_calls=calls,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class ResolveIn(BaseModel):
    """Human verdict on the RCA. Confirmed (or corrected) root causes go into incident memory."""

    correct: bool = True
    # Corrections, if the RCA was wrong or incomplete:
    category: Category | None = None
    service: str | None = Field(default=None, max_length=100)
    root_cause: str | None = Field(default=None, max_length=1000)
    resolution: str = Field(default="", max_length=1000, description="What fixed it")


class WebhookResult(BaseModel):
    created: list[str]
    deduplicated: list[str]


class MemoryEntryOut(BaseModel):
    id: str
    investigation_id: str | None
    title: str
    service: str
    category: str
    root_cause: str
    resolution: str
    similarity: float | None = None
    created_at: str | None = None


class LLMTestOut(BaseModel):
    provider: str
    model: str
    reply: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    cost_usd: float


class HealthOut(BaseModel):
    status: str
    version: str
    llm_provider: str
    reasoner: str
    database: str
