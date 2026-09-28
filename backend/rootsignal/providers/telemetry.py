"""Telemetry provider interface. All methods are read-only.

Phase 2 implements this for Prometheus (metrics) + Loki (logs). AWS CloudWatch can
be added later as another implementation without changing the engine.
"""

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field


class MetricPoint(BaseModel):
    at: datetime
    value: float


class MetricSeries(BaseModel):
    labels: dict[str, str] = Field(default_factory=dict)
    points: list[MetricPoint] = Field(default_factory=list)


class LogLine(BaseModel):
    at: datetime
    service: str
    level: str = "info"
    message: str
    labels: dict[str, str] = Field(default_factory=dict)


class TelemetryError(RuntimeError):
    """The telemetry backend was unreachable, rejected the query, or returned bad data."""


class TelemetryProvider(ABC):
    name: str

    async def aclose(self) -> None:  # noqa: B027 - optional hook, no-op by default
        """Release connections. Called on app shutdown."""

    @abstractmethod
    async def query_metrics(
        self, query: str, start: datetime, end: datetime, step_seconds: int = 30
    ) -> list[MetricSeries]:
        """Run a range query (PromQL for Prometheus)."""

    @abstractmethod
    async def query_logs(
        self, query: str, start: datetime, end: datetime, limit: int = 200
    ) -> list[LogLine]:
        """Run a log query (LogQL for Loki)."""

    @abstractmethod
    async def list_services(self) -> list[str]:
        """Names of the services this backend has data for."""
