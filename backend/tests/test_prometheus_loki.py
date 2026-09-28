"""PrometheusLokiTelemetryProvider against canned Prometheus/Loki HTTP responses."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from rootsignal.providers.prometheus_loki import PrometheusLokiTelemetryProvider
from rootsignal.providers.telemetry import TelemetryError

END = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
START = END - timedelta(minutes=5)


def provider(handler) -> tuple[PrometheusLokiTelemetryProvider, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return PrometheusLokiTelemetryProvider("http://prom:9090/", "http://loki:3100", client), seen


async def test_query_metrics_parses_matrix_and_drops_nan():
    body = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                {
                    "metric": {"service": "checkout"},
                    "values": [[END.timestamp() - 30, "0.25"], [END.timestamp(), "NaN"]],
                }
            ],
        },
    }
    p, seen = provider(lambda _: httpx.Response(200, json=body))
    series = await p.query_metrics("rate(x[1m])", START, END, step_seconds=15)

    assert len(series) == 1
    assert series[0].labels == {"service": "checkout"}
    assert [pt.value for pt in series[0].points] == [0.25]
    req = seen[0]
    assert req.url.path == "/api/v1/query_range"
    assert req.url.params["query"] == "rate(x[1m])"
    assert req.url.params["step"] == "15"
    assert float(req.url.params["end"]) == END.timestamp()


async def test_query_logs_merges_streams_chronologically_and_applies_limit():
    ns = lambda dt: str(int(dt.timestamp() * 1e9))  # noqa: E731
    body = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": "inventory", "level": "error"},
                    "values": [[ns(END), "pool timeout"], [ns(END - timedelta(seconds=20)), "old"]],
                },
                {
                    "stream": {"service": "inventory", "level": "warning"},
                    "values": [[ns(END - timedelta(seconds=10)), "slow cache"]],
                },
            ],
        },
    }
    p, seen = provider(lambda _: httpx.Response(200, json=body))
    lines = await p.query_logs('{service="inventory"}', START, END, limit=2)

    assert [line.message for line in lines] == ["slow cache", "pool timeout"]
    assert lines[1].level == "error" and lines[1].service == "inventory"
    assert seen[0].url.path == "/loki/api/v1/query_range"
    assert seen[0].url.params["direction"] == "backward"
    assert seen[0].url.params["start"] == str(int(START.timestamp() * 1e9))


async def test_list_services():
    body = {"status": "success", "data": ["inventory", "checkout", "gateway"]}
    p, seen = provider(lambda _: httpx.Response(200, json=body))
    assert await p.list_services() == ["checkout", "gateway", "inventory"]
    assert seen[0].url.path == "/api/v1/label/service/values"


async def test_prometheus_error_is_raised_as_telemetry_error():
    body = {"status": "error", "errorType": "bad_data", "error": "parse error at char 5"}
    p, _ = provider(lambda _: httpx.Response(400, json=body))
    with pytest.raises(TelemetryError, match="parse error"):
        await p.query_metrics("rate(", START, END)


async def test_loki_plain_text_error_is_raised_as_telemetry_error():
    p, _ = provider(lambda _: httpx.Response(400, text="parse error : syntax error"))
    with pytest.raises(TelemetryError, match="syntax error"):
        await p.query_logs("{bad", START, END)


async def test_metric_query_sent_to_logs_is_rejected():
    body = {"status": "success", "data": {"resultType": "matrix", "result": []}}
    p, _ = provider(lambda _: httpx.Response(200, json=body))
    with pytest.raises(TelemetryError, match="log query"):
        await p.query_logs('count_over_time({service="x"}[1m])', START, END)


async def test_unreachable_backend_is_raised_as_telemetry_error():
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    p, _ = provider(fail)
    with pytest.raises(TelemetryError, match="failed"):
        await p.list_services()
