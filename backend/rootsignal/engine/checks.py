"""The check catalog: every telemetry question the engine is allowed to ask.

The LLM never writes PromQL or LogQL. It picks a check by name plus a service (and,
for LOG_SEARCH, a sanitized pattern). Code builds the query from a fixed template, runs
it read-only, and turns the result into an `Observation` using fixed thresholds.
"""

import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from rootsignal.core.models import EvidenceKind
from rootsignal.providers.changes import ChangeProvider, ChangeProviderError
from rootsignal.providers.telemetry import MetricSeries, TelemetryError, TelemetryProvider

ERROR_RATE_THRESHOLD = 0.05  # 5% of requests failing
LATENCY_P95_THRESHOLD_S = 0.5
CACHE_P95_THRESHOLD_S = 0.2
MEMORY_THRESHOLD_BYTES = 150 * 1024 * 1024
MEMORY_GROWTH_BYTES = 50 * 1024 * 1024
DB_POOL_SATURATION = 0.9
UPSTREAM_FAILURES_PER_S = 0.05
MIN_LOG_LINES = 3
STEP_SECONDS = 15
MB = 1024 * 1024


class CheckName(StrEnum):
    ERROR_RATE = "error_rate"
    LATENCY_P95 = "latency_p95"
    MEMORY = "memory"
    RESTARTS = "restarts"
    VERSION_CHANGE = "version_change"
    RECENT_CHANGES = "recent_changes"
    DB_POOL = "db_pool"
    CACHE_LATENCY = "cache_latency"
    UPSTREAM_ERRORS = "upstream_errors"
    ERROR_LOGS = "error_logs"
    LOG_SEARCH = "log_search"


CATALOG: dict[CheckName, str] = {
    CheckName.ERROR_RATE: "Share of HTTP requests answered with 5xx.",
    CheckName.LATENCY_P95: "95th percentile HTTP latency.",
    CheckName.MEMORY: "Resident memory: level and growth.",
    CheckName.RESTARTS: "Process restarts (e.g. OOM kills, crashes).",
    CheckName.VERSION_CHANGE: "Running version changed inside the window (from metrics).",
    CheckName.RECENT_CHANGES: "Deploys / config changes recorded for the service.",
    CheckName.DB_POOL: "Database connection pool usage vs. its maximum.",
    CheckName.CACHE_LATENCY: "95th percentile latency of Redis cache calls.",
    CheckName.UPSTREAM_ERRORS: "Failed or timed-out calls from this service to its dependencies.",
    CheckName.ERROR_LOGS: "Error log volume and the most frequent error messages.",
    CheckName.LOG_SEARCH: "Warning/error log lines matching a pattern (case-insensitive).",
}

# How much to trust each check as evidence (0-1). Direct signals beat generic symptoms.
RELIABILITY: dict[CheckName, float] = {
    CheckName.ERROR_RATE: 0.5,
    CheckName.LATENCY_P95: 0.5,
    CheckName.MEMORY: 0.8,
    CheckName.RESTARTS: 0.7,
    CheckName.VERSION_CHANGE: 0.9,
    CheckName.RECENT_CHANGES: 0.8,
    CheckName.DB_POOL: 0.9,
    CheckName.CACHE_LATENCY: 0.8,
    CheckName.UPSTREAM_ERRORS: 0.6,
    CheckName.ERROR_LOGS: 0.6,
    CheckName.LOG_SEARCH: 0.8,
}

KIND: dict[CheckName, EvidenceKind] = {
    CheckName.RECENT_CHANGES: EvidenceKind.CHANGE,
    CheckName.ERROR_LOGS: EvidenceKind.LOG,
    CheckName.LOG_SEARCH: EvidenceKind.LOG,
}

_SERVICE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_PATTERN_RE = re.compile(r"^[\w\s\-':.|,/]{1,80}$")


class Observation(BaseModel):
    """What a check found. `available=False` means no data, which is not evidence either way."""

    check: CheckName
    service: str
    pattern: str | None = None
    available: bool = True
    anomalous: bool = False
    severity: float = Field(default=0.0, ge=0.0, le=1.0)
    summary: str
    query: str = ""
    started_at: datetime | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return check_key(self.check, self.service, self.pattern)


def check_key(check: CheckName, service: str, pattern: str | None) -> tuple[str, str, str]:
    return (check.value, service, (pattern or "").lower())


def valid_service(service: str) -> bool:
    return bool(_SERVICE_RE.match(service))


def sanitize_pattern(pattern: str | None) -> str | None:
    """Accept only short, plain patterns ('|' allowed for alternatives). None if unsafe."""
    if not pattern:
        return None
    pattern = pattern.strip()
    return pattern if _PATTERN_RE.match(pattern) else None


@dataclass
class CheckContext:
    telemetry: TelemetryProvider
    changes: ChangeProvider | None
    start: datetime
    end: datetime
    changes_since: datetime


# --------------------------------------------------------------------------- helpers


def _peak(series: list[MetricSeries]) -> tuple[float, datetime | None]:
    best, at = float("-inf"), None
    for s in series:
        for p in s.points:
            if p.value > best:
                best, at = p.value, p.at
    return (best if at else 0.0), at


def _first_above(series: list[MetricSeries], threshold: float) -> datetime | None:
    times = [p.at for s in series for p in s.points if p.value > threshold]
    return min(times) if times else None


def _has_points(series: list[MetricSeries]) -> bool:
    return any(s.points for s in series)


def _no_data(check: CheckName, service: str, query: str, why: str = "no data") -> Observation:
    return Observation(
        check=check, service=service, available=False, summary=f"{service}: {why}", query=query
    )


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%H:%M:%S") if dt else "?"


def log_signature(line: str) -> str:
    """Collapse a JSON log line to 'msg: ExceptionType: detail' for grouping."""
    try:
        payload = json.loads(line)
    except ValueError:
        return line[:160]
    msg = str(payload.get("msg", ""))
    exc = str(payload.get("exception", "")).strip().splitlines()
    if exc:
        msg = f"{msg}: {exc[-1]}"
    for key in ("target", "status", "cache", "op"):
        if key in payload:
            msg += f" [{key}={payload[key]}]"
    return msg[:200]


# --------------------------------------------------------------------------- checks


async def _metric_threshold(
    ctx: CheckContext,
    check: CheckName,
    service: str,
    query: str,
    threshold: float,
    fmt: Callable[[float], str],
    what: str,
    severity_scale: float,
) -> Observation:
    series = await ctx.telemetry.query_metrics(query, ctx.start, ctx.end, STEP_SECONDS)
    if not _has_points(series):
        return _no_data(check, service, query)
    peak, peak_at = _peak(series)
    anomalous = peak > threshold
    return Observation(
        check=check,
        service=service,
        anomalous=anomalous,
        severity=min(1.0, peak / severity_scale) if anomalous else 0.0,
        summary=(
            f"{service} {what} peaked at {fmt(peak)} (threshold {fmt(threshold)})"
            + (
                f", above threshold since {_fmt_time(_first_above(series, threshold))}"
                if anomalous
                else ""
            )
        ),
        query=query,
        started_at=_first_above(series, threshold),
        data={"peak": peak, "peak_at": peak_at.isoformat() if peak_at else None},
    )


async def error_rate(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = (
        f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m]))'
        f' / sum(rate(http_requests_total{{service="{service}"}}[1m]))'
    )
    return await _metric_threshold(
        ctx, CheckName.ERROR_RATE, service, q, ERROR_RATE_THRESHOLD, _pct, "5xx ratio", 0.3
    )


async def latency_p95(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = (
        "histogram_quantile(0.95, sum by (le) "
        f'(rate(http_request_duration_seconds_bucket{{service="{service}"}}[1m])))'
    )
    return await _metric_threshold(
        ctx,
        CheckName.LATENCY_P95,
        service,
        q,
        LATENCY_P95_THRESHOLD_S,
        lambda v: f"{v:.2f}s",
        "p95 latency",
        2.0,
    )


async def cache_latency(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = (
        "histogram_quantile(0.95, sum by (le) "
        f'(rate(cache_request_duration_seconds_bucket{{service="{service}"}}[1m])))'
    )
    return await _metric_threshold(
        ctx,
        CheckName.CACHE_LATENCY,
        service,
        q,
        CACHE_P95_THRESHOLD_S,
        lambda v: f"{v:.2f}s",
        "cache p95 latency",
        1.5,
    )


async def memory(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = f'process_resident_memory_bytes{{service="{service}"}}'
    series = await ctx.telemetry.query_metrics(q, ctx.start, ctx.end, STEP_SECONDS)
    points = sorted((p for s in series for p in s.points), key=lambda p: p.at)
    if not points:
        return _no_data(CheckName.MEMORY, service, q)
    peak = max(p.value for p in points)
    low_before_peak = min(p.value for p in points if p.at <= max(points, key=lambda p: p.value).at)
    growth = peak - low_before_peak
    anomalous = peak > MEMORY_THRESHOLD_BYTES or growth > MEMORY_GROWTH_BYTES
    started = next(
        (p.at for p in points if p.value > low_before_peak + MEMORY_GROWTH_BYTES / 2), None
    )
    return Observation(
        check=CheckName.MEMORY,
        service=service,
        anomalous=anomalous,
        severity=min(1.0, max(peak / (2 * MEMORY_THRESHOLD_BYTES), growth / (100 * MB)))
        if anomalous
        else 0.0,
        summary=(
            f"{service} memory peaked at {peak / MB:.0f}MB, "
            f"growing {growth / MB:.0f}MB in the window"
            f" (threshold {MEMORY_THRESHOLD_BYTES / MB:.0f}MB or +{MEMORY_GROWTH_BYTES / MB:.0f}MB)"
        ),
        query=q,
        started_at=started if anomalous else None,
        data={"peak_mb": round(peak / MB, 1), "growth_mb": round(growth / MB, 1)},
    )


async def restarts(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = f'process_start_time_seconds{{service="{service}"}}'
    series = await ctx.telemetry.query_metrics(q, ctx.start, ctx.end, STEP_SECONDS)
    points = sorted((p for s in series for p in s.points), key=lambda p: p.at)
    if not points:
        return _no_data(CheckName.RESTARTS, service, q)
    starts = sorted({round(p.value) for p in points})
    count = len(starts) - 1
    first_restart = next((p.at for p in points if round(p.value) != starts[0]), None)
    return Observation(
        check=CheckName.RESTARTS,
        service=service,
        anomalous=count > 0,
        severity=min(1.0, 0.6 + 0.2 * count) if count else 0.0,
        summary=f"{service} restarted {count} time(s) in the window"
        + (f", first at {_fmt_time(first_restart)}" if count else ""),
        query=q,
        started_at=first_restart,
        data={"restarts": count},
    )


async def version_change(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = f'app_build_info{{service="{service}"}}'
    series = await ctx.telemetry.query_metrics(q, ctx.start, ctx.end, STEP_SECONDS)
    seen: list[tuple[datetime, str]] = []
    for s in series:
        if s.points and "version" in s.labels:
            seen.append((min(p.at for p in s.points), s.labels["version"]))
    if not seen:
        return _no_data(CheckName.VERSION_CHANGE, service, q)
    seen.sort()
    versions = [v for _, v in seen]
    changed = len(versions) > 1
    return Observation(
        check=CheckName.VERSION_CHANGE,
        service=service,
        anomalous=changed,
        severity=1.0 if changed else 0.0,
        summary=(
            f"{service} version changed {' -> '.join(versions)} at {_fmt_time(seen[-1][0])}"
            if changed
            else f"{service} ran version {versions[0]} throughout the window"
        ),
        query=q,
        started_at=seen[-1][0] if changed else None,
        data={"versions": versions},
    )


async def recent_changes(ctx: CheckContext, service: str, _: str | None) -> Observation:
    query = f"changes(service={service}, since={ctx.changes_since.isoformat()})"
    if ctx.changes is None:
        return _no_data(CheckName.RECENT_CHANGES, service, query, "no change source configured")
    try:
        changes = await ctx.changes.recent_changes(service, ctx.changes_since)
    except ChangeProviderError as exc:
        return _no_data(CheckName.RECENT_CHANGES, service, query, f"change source failed: {exc}")
    changes.sort(key=lambda c: c.at)
    latest = changes[-1] if changes else None
    return Observation(
        check=CheckName.RECENT_CHANGES,
        service=service,
        anomalous=bool(changes),
        severity=0.8 if changes else 0.0,
        summary=(
            f"{len(changes)} change(s) to {service}; latest: {latest.kind} '{latest.summary}'"
            f" at {_fmt_time(latest.at)}"
            if latest
            else f"no deploys or config changes to {service} in the last "
            f"{int((ctx.end - ctx.changes_since).total_seconds() // 60)} min"
        ),
        query=query,
        started_at=latest.at if latest else None,
        data={"changes": [c.model_dump(mode="json") for c in changes[-5:]]},
    )


async def db_pool(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = (
        f'max(db_pool_connections_in_use{{service="{service}"}})'
        f' / max(db_pool_connections_max{{service="{service}"}})'
    )
    return await _metric_threshold(
        ctx,
        CheckName.DB_POOL,
        service,
        q,
        DB_POOL_SATURATION - 1e-9,
        _pct,
        "DB pool usage",
        1.0,
    )


async def upstream_errors(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = f'sum by (target) (rate(upstream_requests_total{{service="{service}",outcome!="ok"}}[1m]))'
    series = await ctx.telemetry.query_metrics(q, ctx.start, ctx.end, STEP_SECONDS)
    if not _has_points(series):
        return Observation(
            check=CheckName.UPSTREAM_ERRORS,
            service=service,
            summary=f"no failed calls from {service} to its dependencies",
            query=q,
        )
    by_target = {
        s.labels.get("target", "?"): max(p.value for p in s.points) for s in series if s.points
    }
    failing = {t: v for t, v in by_target.items() if v > UPSTREAM_FAILURES_PER_S}
    return Observation(
        check=CheckName.UPSTREAM_ERRORS,
        service=service,
        anomalous=bool(failing),
        severity=min(1.0, max(failing.values(), default=0) / 1.0),
        summary=(
            f"{service} calls failing: "
            + ", ".join(f"{t} peaked at {v:.2f}/s" for t, v in sorted(failing.items()))
            if failing
            else f"no significant failed calls from {service} to its dependencies"
        ),
        query=q,
        started_at=_first_above(series, UPSTREAM_FAILURES_PER_S),
        data={"failing_targets": sorted(failing), "peak_per_target": by_target},
    )


async def _log_check(
    ctx: CheckContext, check: CheckName, service: str, query: str, what: str, pattern: str | None
) -> Observation:
    lines = await ctx.telemetry.query_logs(query, ctx.start, ctx.end, limit=500)
    top = Counter(log_signature(line.message) for line in lines).most_common(3)
    anomalous = len(lines) >= MIN_LOG_LINES
    scale = 20 if check == CheckName.ERROR_LOGS else 5
    return Observation(
        check=check,
        service=service,
        pattern=pattern,
        anomalous=anomalous,
        severity=min(1.0, len(lines) / scale) if anomalous else 0.0,
        summary=(
            f"{len(lines)} {what} for {service}"
            + ("; most common: " + "; ".join(f"'{m}' x{n}" for m, n in top) if top else "")
        ),
        query=query,
        started_at=lines[0].at if anomalous else None,
        data={"count": len(lines), "top_messages": [{"message": m, "count": n} for m, n in top]},
    )


async def error_logs(ctx: CheckContext, service: str, _: str | None) -> Observation:
    q = f'{{service="{service}", level="error"}}'
    return await _log_check(ctx, CheckName.ERROR_LOGS, service, q, "error log lines", None)


async def log_search(ctx: CheckContext, service: str, pattern: str | None) -> Observation:
    safe = sanitize_pattern(pattern)
    if safe is None:
        return _no_data(CheckName.LOG_SEARCH, service, "", f"rejected unsafe pattern {pattern!r}")
    escaped = safe.replace("\\", "\\\\").replace('"', '\\"')
    q = f'{{service="{service}", level=~"warning|error"}} |~ "(?i){escaped}"'
    return await _log_check(
        ctx, CheckName.LOG_SEARCH, service, q, f"warning/error lines matching '{safe}'", safe
    )


RUNNERS: dict[CheckName, Callable[[CheckContext, str, str | None], Awaitable[Observation]]] = {
    CheckName.ERROR_RATE: error_rate,
    CheckName.LATENCY_P95: latency_p95,
    CheckName.MEMORY: memory,
    CheckName.RESTARTS: restarts,
    CheckName.VERSION_CHANGE: version_change,
    CheckName.RECENT_CHANGES: recent_changes,
    CheckName.DB_POOL: db_pool,
    CheckName.CACHE_LATENCY: cache_latency,
    CheckName.UPSTREAM_ERRORS: upstream_errors,
    CheckName.ERROR_LOGS: error_logs,
    CheckName.LOG_SEARCH: log_search,
}


async def run_check(
    ctx: CheckContext, check: CheckName, service: str, pattern: str | None = None
) -> Observation:
    """Run one check. Never raises: backend failures become unavailable observations."""
    if not valid_service(service):
        return _no_data(check, service, "", "invalid service name")
    try:
        return await RUNNERS[check](ctx, service, pattern)
    except TelemetryError as exc:
        return _no_data(check, service, "", f"telemetry query failed: {exc}")


def window(
    end: datetime, lookback_minutes: int, changes_lookback_minutes: int
) -> tuple[datetime, datetime, datetime]:
    return (
        end - timedelta(minutes=lookback_minutes),
        end,
        end - timedelta(minutes=changes_lookback_minutes),
    )
