"""Shared plumbing for the demo services: settings, JSON logs (-> Loki), metrics, upstream calls."""

import asyncio
import json
import logging
import os
import queue
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from redis.asyncio import Redis

SKUS = [f"SKU-{i:03d}" for i in range(1, 21)]

# --------------------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    service: str
    redis_url: str
    loki_url: str  # empty = don't ship logs to Loki (tests, local runs)
    checkout_url: str
    inventory_url: str
    inventory_db_url: str
    db_pool_size: int
    fault_poll_seconds: float

    @classmethod
    def from_env(cls, service: str) -> "Settings":
        return cls(
            service=service,
            redis_url=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
            loki_url=os.getenv("LOKI_URL", ""),
            checkout_url=os.getenv("CHECKOUT_URL", "http://localhost:8001"),
            inventory_url=os.getenv("INVENTORY_URL", "http://localhost:8002"),
            inventory_db_url=os.getenv(
                "INVENTORY_DB_URL", "postgresql://inventory:inventory@localhost:5433/inventory"
            ),
            db_pool_size=int(os.getenv("DB_POOL_SIZE", "5")),
            fault_poll_seconds=float(os.getenv("FAULT_POLL_SECONDS", "2")),
        )


def make_redis(url: str) -> Redis:
    return Redis.from_url(url, decode_responses=True, socket_timeout=2)


# --------------------------------------------------------------------------- version

BUILD_INFO = Gauge(
    "app_build_info", "Running version of the service (value is always 1)", ["version"]
)
_version = "0.0.0"


def current_version() -> str:
    return _version


def set_version(version: str) -> None:
    """Report the running version in logs and in the app_build_info metric."""
    global _version
    _version = version
    BUILD_INFO.clear()
    BUILD_INFO.labels(version=version).set(1)


# --------------------------------------------------------------------------- logging


def fields(**kwargs: Any) -> dict[str, Any]:
    """Structured log fields: log.info("order created", extra=fields(order_id=...))."""
    return {"fields": kwargs}


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "service": self.service,
            "version": current_version(),
            "msg": record.getMessage(),
        }
        payload.update(getattr(record, "fields", {}))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class LokiHandler(logging.Handler):
    """Ships log lines to Loki's push API from a background thread.

    Never blocks or breaks the request path: if Loki is slow or down, lines are buffered
    (up to a cap) and retried, then the oldest are dropped.
    """

    def __init__(self, url: str, service: str, flush_seconds: float = 1.0, max_buffer: int = 5000):
        super().__init__()
        self._push_url = url.rstrip("/") + "/loki/api/v1/push"
        self._service = service
        self._flush_seconds = flush_seconds
        self._max_buffer = max_buffer
        self._queue: queue.Queue[tuple[str, str, str]] = queue.Queue(maxsize=max_buffer)
        threading.Thread(target=self._run, name="loki-shipper", daemon=True).start()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            ts_ns = str(int(record.created * 1e9))
            self._queue.put_nowait((ts_ns, record.levelname.lower(), self.format(record)))
        except queue.Full:
            pass  # drop rather than slow down the service
        except Exception:
            self.handleError(record)

    def _run(self) -> None:
        pending: list[tuple[str, str, str]] = []
        with httpx.Client(timeout=5.0) as client:
            while True:
                time.sleep(self._flush_seconds)
                while True:
                    try:
                        pending.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                if not pending:
                    continue
                pending = pending[-self._max_buffer :]
                streams: dict[str, list[list[str]]] = {}
                for ts_ns, level, line in pending:
                    streams.setdefault(level, []).append([ts_ns, line])
                body = {
                    "streams": [
                        {"stream": {"service": self._service, "level": level}, "values": values}
                        for level, values in streams.items()
                    ]
                }
                try:
                    client.post(self._push_url, json=body).raise_for_status()
                    pending = []
                except httpx.HTTPError:
                    pass  # Loki not ready yet or down; retry next round


def setup_logging(settings: Settings) -> logging.Logger:
    formatter = JsonFormatter(settings.service)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(formatter)
    root.addHandler(stdout)

    if settings.loki_url:
        loki = LokiHandler(settings.loki_url, settings.service)
        loki.setFormatter(formatter)
        root.addHandler(loki)

    # Our middleware logs every request as JSON; silence the duplicate plain-text logs.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(settings.service)


# --------------------------------------------------------------------------- metrics

REQUESTS = Counter("http_requests_total", "HTTP requests served", ["route", "method", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["route", "method"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
UPSTREAM = Counter("upstream_requests_total", "Calls to other services", ["target", "outcome"])
UPSTREAM_LATENCY = Histogram(
    "upstream_request_duration_seconds",
    "Latency of calls to other services",
    ["target"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)

UNINSTRUMENTED_PATHS = {"/metrics", "/health"}


def instrument(app: FastAPI, log: logging.Logger) -> None:
    """Record metrics and a JSON log line for every request; turn crashes into logged 500s."""

    @app.middleware("http")
    async def observe(request: Request, call_next: Callable) -> Response:
        if request.url.path in UNINSTRUMENTED_PATHS:
            return await call_next(request)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "unhandled error", extra=fields(method=request.method, path=request.url.path)
            )
            response = JSONResponse({"error": "internal server error"}, status_code=500)
        elapsed = time.perf_counter() - start

        # Use the route template ("/stock/{sku}") so metric labels stay low-cardinality.
        route = getattr(request.scope.get("route"), "path", "unmatched")
        status = response.status_code
        REQUESTS.labels(route=route, method=request.method, status=str(status)).inc()
        LATENCY.labels(route=route, method=request.method).observe(elapsed)

        level = (
            logging.ERROR if status >= 500 else logging.WARNING if status >= 400 else logging.INFO
        )
        log.log(
            level,
            "request",
            extra=fields(
                method=request.method,
                route=route,
                path=request.url.path,
                status=status,
                duration_ms=round(elapsed * 1000, 1),
            ),
        )
        return response


# --------------------------------------------------------------------------- upstream calls


class UpstreamError(Exception):
    def __init__(self, target: str, status_code: int, message: str):
        super().__init__(message)
        self.target = target
        self.status_code = status_code
        self.message = message


async def call_upstream(
    client: httpx.AsyncClient,
    log: logging.Logger,
    target: str,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Call another service. 5xx/timeouts are logged and raised as UpstreamError (502/504)."""
    start = time.perf_counter()
    try:
        resp = await client.request(method, url, **kwargs)
    except httpx.TimeoutException as exc:
        UPSTREAM.labels(target=target, outcome="timeout").inc()
        log.error("upstream timeout", extra=fields(target=target, url=url))
        raise UpstreamError(target, 504, f"{target} timed out") from exc
    except httpx.HTTPError as exc:
        UPSTREAM.labels(target=target, outcome="unreachable").inc()
        log.error("upstream unreachable", extra=fields(target=target, url=url, error=str(exc)))
        raise UpstreamError(target, 502, f"{target} unreachable") from exc
    finally:
        UPSTREAM_LATENCY.labels(target=target).observe(time.perf_counter() - start)

    if resp.status_code >= 500:
        UPSTREAM.labels(target=target, outcome="error").inc()
        log.error("upstream error", extra=fields(target=target, url=url, status=resp.status_code))
        raise UpstreamError(target, 502, f"{target} returned {resp.status_code}")
    UPSTREAM.labels(target=target, outcome="ok").inc()
    return resp


# --------------------------------------------------------------------------- app factory


def create_service_app(
    settings: Settings,
    version: str,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> tuple[FastAPI, logging.Logger]:
    log = setup_logging(settings)
    set_version(version)
    app = FastAPI(title=f"demo-{settings.service}", lifespan=lifespan)
    instrument(app, log)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": settings.service, "version": current_version()}

    @app.exception_handler(UpstreamError)
    async def upstream_error(_: Request, exc: UpstreamError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)

    return app, log


def rss_bytes() -> int:
    """Resident memory of this process (Linux). 0 if unavailable."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def start_periodic(interval: float, fn: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
    """Run `fn` every `interval` seconds until the task is cancelled. Errors are logged."""

    async def loop() -> None:
        while True:
            try:
                await fn()
            except Exception:
                logging.getLogger(__name__).exception("periodic task failed")
            await asyncio.sleep(interval)

    return asyncio.create_task(loop())
