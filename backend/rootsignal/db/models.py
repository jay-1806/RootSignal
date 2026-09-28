import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from rootsignal.core.models import utcnow
from rootsignal.core.state_machine import InvestigationState

EMBEDDING_DIM = 256

# JSONB on Postgres, plain JSON elsewhere (SQLite in tests).
JsonType = JSON().with_variant(JSONB(), "postgresql")
# pgvector on Postgres; a JSON list on SQLite, where similarity is computed in Python.
EmbeddingType = JSON().with_variant(Vector(EMBEDDING_DIM), "postgresql")


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class InvestigationRow(Base):
    __tablename__ = "investigations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(String(300))
    service: Mapped[str] = mapped_column(String(100), index=True)
    severity: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(50))
    state: Mapped[str] = mapped_column(
        String(30), index=True, default=InvestigationState.RECEIVED.value
    )
    alert: Mapped[dict[str, Any]] = mapped_column(JsonType)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    hypotheses: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, default=list)
    rca: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    # Added in Phase 3 (migration 0002)
    context: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    iterations: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    reasoner: Mapped[str] = mapped_column(String(100), default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    llm_calls: Mapped[list["LLMCallRow"]] = relationship(
        back_populates="investigation",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="LLMCallRow.created_at",
    )


class LLMCallRow(Base):
    """One LLM request made during an investigation: for cost, latency, and failure tracking."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(
        ForeignKey("investigations.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(50))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    investigation: Mapped[InvestigationRow] = relationship(back_populates="llm_calls")


class IncidentMemoryRow(Base):
    """A human-confirmed root cause, searchable by symptom similarity."""

    __tablename__ = "incident_memory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str | None] = mapped_column(
        ForeignKey("investigations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(300))
    service: Mapped[str] = mapped_column(String(100), index=True)
    category: Mapped[str] = mapped_column(String(50))
    root_cause: Mapped[str] = mapped_column(Text)
    resolution: Mapped[str] = mapped_column(Text, default="")
    signature: Mapped[str] = mapped_column(Text)  # the symptom text that was embedded
    embedding: Mapped[list[float]] = mapped_column(EmbeddingType)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
