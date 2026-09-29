"""Scripted telemetry for the demo's fault scenarios.

`ScenarioTelemetry` answers the engine's PromQL/LogQL by recognizing the metric name and
the service label, and returns a 15-minute series (60 points, 15s apart) ending at
`now`. This lets tests run the whole investigation loop offline and deterministically.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from rootsignal.providers.changes import Change, ChangeKind, ChangeProvider
from rootsignal.providers.telemetry import LogLine, MetricPoint, MetricSeries, TelemetryProvider

NOW = datetime.now(UTC).replace(microsecond=0)
POINTS = 60
MB = 1024 * 1024


def ramp(before: float, after: float, at: int = 40, n: int = POINTS) -> list[float]:
    """`before` for the first `at` points, then `after`."""
    return [before] * at + [after] * (n - at)


def grow(start: float, end: float, n: int = POINTS) -> list[float]:
    return [start + (end - start) * i / (n - 1) for i in range(n)]


def flat(v: float) -> list[float]:
    return [v] * POINTS


METRIC_KEYS = [
    ('status=~"5.."', "error_rate"),
    ("http_request_duration_seconds_bucket", "latency_p95"),
    ("process_resident_memory_bytes", "memory"),
    ("process_start_time_seconds", "start_time"),
    ("app_build_info", "versions"),
    ("db_pool_connections_in_use", "db_pool"),
    ("cache_request_duration_seconds_bucket", "cache_p95"),
    ("upstream_requests_total", "upstream"),
]


def healthy_metrics() -> dict[tuple[str, str], object]:
    m: dict[tuple[str, str], object] = {}
    for svc, version in (("gateway", "2.3.1"), ("checkout", "1.8.0"), ("inventory", "3.0.4")):
        m[("error_rate", svc)] = flat(0.0)
        m[("latency_p95", svc)] = flat(0.05)
        m[("memory", svc)] = flat(60 * MB)
        m[("start_time", svc)] = flat(1_700_000_000)
        m[("versions", svc)] = {version: (0, POINTS)}
        m[("upstream", svc)] = {}
    m[("db_pool", "inventory")] = flat(0.2)
    m[("cache_p95", "inventory")] = flat(0.003)
    return m


@dataclass
class ScenarioTelemetry(TelemetryProvider):
    metrics: dict = field(default_factory=healthy_metrics)
    # service -> list of (level, payload dict, count)
    logs: dict[str, list[tuple[str, dict, int]]] = field(default_factory=dict)
    now: datetime = NOW
    queries: list[str] = field(default_factory=list)
    name: str = "scenario"

    def _times(self) -> list[datetime]:
        return [self.now - timedelta(seconds=15 * (POINTS - 1 - i)) for i in range(POINTS)]

    def _series(self, values: list[float], labels: dict | None = None) -> MetricSeries:
        return MetricSeries(
            labels=labels or {},
            points=[MetricPoint(at=t, value=v) for t, v in zip(self._times(), values, strict=True)],
        )

    async def query_metrics(self, query, start, end, step_seconds=30):
        self.queries.append(query)
        service = re.search(r'service="([^"]+)"', query).group(1)
        key = next((k for needle, k in METRIC_KEYS if needle in query), None)
        value = self.metrics.get((key, service))
        if value is None:
            return []
        if key == "versions":
            times = self._times()
            return [
                MetricSeries(
                    labels={"version": v, "service": service},
                    points=[MetricPoint(at=times[i], value=1) for i in range(a, b)],
                )
                for v, (a, b) in value.items()
            ]
        if key == "upstream":
            return [self._series(vals, {"target": t}) for t, vals in value.items()]
        return [self._series(value)]

    async def query_logs(self, query, start, end, limit=200):
        self.queries.append(query)
        service = re.search(r'service="([^"]+)"', query).group(1)
        levels = {"error"} if 'level="error"' in query else {"warning", "error"}
        m = re.search(r'\|~ "\(\?i\)(.*)"$', query)
        pattern = re.compile(m.group(1), re.I) if m else None
        lines = []
        for level, payload, count in self.logs.get(service, []):
            text = json.dumps({"level": level, "service": service, **payload})
            if level in levels and (pattern is None or pattern.search(text)):
                for i in range(count):
                    at = self.now - timedelta(seconds=5 * (count - i))
                    lines.append(LogLine(at=at, service=service, level=level, message=text))
        return sorted(lines, key=lambda x: x.at)[-limit:]

    async def list_services(self):
        return ["checkout", "gateway", "inventory"]


@dataclass
class FakeChanges(ChangeProvider):
    changes: list[Change] = field(default_factory=list)
    name: str = "fake_changes"

    async def recent_changes(self, service, since):
        return [
            c for c in self.changes if (service is None or c.service == service) and c.at >= since
        ]


# --------------------------------------------------------------------------- scenarios


def bad_deploy() -> tuple[ScenarioTelemetry, FakeChanges]:
    t = ScenarioTelemetry()
    t.metrics[("error_rate", "checkout")] = ramp(0.0, 0.4)
    t.metrics[("versions", "checkout")] = {"1.8.0": (0, 40), "1.9.0": (40, POINTS)}
    t.metrics[("error_rate", "gateway")] = ramp(0.0, 0.12)
    t.metrics[("upstream", "gateway")] = {"checkout": ramp(0.0, 0.6)}
    t.logs["checkout"] = [
        (
            "error",
            {"msg": "unhandled error", "exception": "Traceback\nKeyError: 'discount_code'"},
            25,
        )
    ]
    t.logs["gateway"] = [
        ("error", {"msg": "upstream error", "target": "checkout", "status": 500}, 15)
    ]
    deploy = Change(
        kind=ChangeKind.DEPLOY,
        service="checkout",
        at=NOW - timedelta(minutes=5),
        summary="checkout 1.8.0 -> 1.9.0",
        ref="checkout:1.9.0",
    )
    return t, FakeChanges([deploy])


def db_pool_exhaustion() -> tuple[ScenarioTelemetry, FakeChanges]:
    t = ScenarioTelemetry()
    t.metrics[("db_pool", "inventory")] = ramp(0.2, 1.0)
    t.metrics[("error_rate", "inventory")] = ramp(0.0, 0.3)
    t.metrics[("latency_p95", "inventory")] = ramp(0.05, 1.1)
    t.metrics[("error_rate", "checkout")] = ramp(0.0, 0.2)
    t.metrics[("upstream", "checkout")] = {"inventory": ramp(0.0, 0.5)}
    t.logs["inventory"] = [
        ("error", {"msg": "timed out acquiring database connection", "pool_max": 5}, 30)
    ]
    t.logs["checkout"] = [
        ("error", {"msg": "upstream error", "target": "inventory", "status": 503}, 10)
    ]
    return t, FakeChanges()


def latency_spike() -> tuple[ScenarioTelemetry, FakeChanges]:
    t = ScenarioTelemetry()
    t.metrics[("latency_p95", "inventory")] = ramp(0.05, 1.4)
    t.metrics[("cache_p95", "inventory")] = ramp(0.003, 1.3)
    t.metrics[("latency_p95", "gateway")] = ramp(0.06, 1.5)
    t.logs["inventory"] = [
        ("warning", {"msg": "slow cache response", "cache": "redis", "op": "get"}, 40)
    ]
    return t, FakeChanges()


def memory_leak() -> tuple[ScenarioTelemetry, FakeChanges]:
    t = ScenarioTelemetry()
    t.metrics[("memory", "checkout")] = grow(60 * MB, 180 * MB)
    t.logs["checkout"] = [("warning", {"msg": "memory usage", "rss_mb": 170.2}, 4)]
    return t, FakeChanges()


def healthy() -> tuple[ScenarioTelemetry, FakeChanges]:
    return ScenarioTelemetry(), FakeChanges()
