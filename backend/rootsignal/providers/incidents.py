"""Incident source interface: turn a raw webhook payload into normalized alerts."""

from abc import ABC, abstractmethod
from typing import Any

from rootsignal.core.models import Alert


class IncidentSource(ABC):
    name: str  # e.g. "alertmanager"

    @abstractmethod
    def parse(self, payload: dict[str, Any]) -> list[Alert]:
        """Return the firing alerts in `payload`. Resolved alerts should be skipped."""
