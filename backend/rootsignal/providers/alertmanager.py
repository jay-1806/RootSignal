"""IncidentSource for Prometheus Alertmanager webhooks.

Payload format: https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
"""

from datetime import UTC, datetime
from typing import Any

from rootsignal.core.models import Alert, Severity
from rootsignal.providers.incidents import IncidentSource

SEVERITY_MAP = {
    "critical": Severity.CRITICAL,
    "page": Severity.CRITICAL,
    "high": Severity.HIGH,
    "error": Severity.HIGH,
    "warning": Severity.MEDIUM,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
    "info": Severity.LOW,
}


def _parse_time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)


class AlertmanagerSource(IncidentSource):
    name = "alertmanager"

    def parse(self, payload: dict[str, Any]) -> list[Alert]:
        alerts = []
        for raw in payload.get("alerts", []):
            if raw.get("status") != "firing":
                continue
            labels = {str(k): str(v) for k, v in (raw.get("labels") or {}).items()}
            annotations = raw.get("annotations") or {}
            alerts.append(
                Alert(
                    title=labels.get("alertname", "unnamed alert")[:300],
                    service=(labels.get("service") or labels.get("job") or "unknown")[:100],
                    severity=SEVERITY_MAP.get(labels.get("severity", "").lower(), Severity.HIGH),
                    source=self.name,
                    description=str(
                        annotations.get("summary") or annotations.get("description") or ""
                    )[:1000],
                    labels=labels,
                    fired_at=_parse_time(raw.get("startsAt")),
                )
            )
        return alerts
