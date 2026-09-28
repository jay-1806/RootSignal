"""TelemetryProvider backed by Prometheus (metrics) and Loki (logs). Read-only HTTP queries."""

import math
from datetime import UTC, datetime
from typing import Any

import httpx

from rootsignal.config import Settings
from rootsignal.providers.telemetry import (
    LogLine,
    MetricPoint,
    MetricSeries,
    TelemetryError,
    TelemetryProvider,
)


class PrometheusLokiTelemetryProvider(TelemetryProvider):
    name = "prometheus_loki"

    def __init__(
        self,
        prometheus_url: str,
        loki_url: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
    ):
        self._prometheus = prometheus_url.rstrip("/")
        self._loki = loki_url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict[str, Any]) -> Any:
        """GET a Prometheus/Loki API endpoint and return its `data` field."""
        try:
            resp = await self._client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise TelemetryError(f"request to {url} failed: {exc!r}") from exc
        try:
            body = resp.json()
        except ValueError:
            # Loki returns plain-text errors for bad queries.
            raise TelemetryError(f"{url} returned {resp.status_code}: {resp.text[:300]}") from None
        if resp.status_code >= 400 or body.get("status") != "success":
            raise TelemetryError(f"{url} returned {resp.status_code}: {body.get('error', body)}")
        return body["data"]

    async def query_metrics(
        self, query: str, start: datetime, end: datetime, step_seconds: int = 30
    ) -> list[MetricSeries]:
        data = await self._get(
            f"{self._prometheus}/api/v1/query_range",
            {
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step_seconds,
            },
        )
        if data.get("resultType") != "matrix":
            raise TelemetryError(f"expected a range (matrix) result, got {data.get('resultType')}")
        series = []
        for result in data["result"]:
            points = []
            for ts, raw in result["values"]:
                value = float(raw)
                if math.isfinite(value):  # drop NaN/Inf (e.g. 0/0 error ratios)
                    points.append(MetricPoint(at=datetime.fromtimestamp(ts, UTC), value=value))
            series.append(MetricSeries(labels=result["metric"], points=points))
        return series

    async def query_logs(
        self, query: str, start: datetime, end: datetime, limit: int = 200
    ) -> list[LogLine]:
        """Return up to `limit` of the newest matching lines, in chronological order."""
        data = await self._get(
            f"{self._loki}/loki/api/v1/query_range",
            {
                "query": query,
                "start": _to_ns(start),
                "end": _to_ns(end),
                "limit": limit,
                "direction": "backward",
            },
        )
        if data.get("resultType") != "streams":
            raise TelemetryError(
                f"expected a log query (streams), got {data.get('resultType')}; "
                "use query_metrics for metric queries"
            )
        lines = []
        for stream in data["result"]:
            labels = stream["stream"]
            for ts_ns, message in stream["values"]:
                lines.append(
                    LogLine(
                        at=datetime.fromtimestamp(int(ts_ns) / 1e9, UTC),
                        service=labels.get("service", ""),
                        level=labels.get("level", "info"),
                        message=message,
                        labels=labels,
                    )
                )
        lines.sort(key=lambda line: line.at)
        return lines[-limit:]

    async def list_services(self) -> list[str]:
        data = await self._get(f"{self._prometheus}/api/v1/label/service/values", {})
        return sorted(data)


def _to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1_000_000_000)


def build_telemetry_provider(settings: Settings) -> TelemetryProvider:
    return PrometheusLokiTelemetryProvider(settings.prometheus_url, settings.loki_url)
