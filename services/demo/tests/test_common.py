import json
import logging

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from demo.common import (
    JsonFormatter,
    Settings,
    call_upstream,
    create_service_app,
    fields,
    set_version,
)

app, log = create_service_app(Settings.from_env("testsvc"), "0.1.0")


@app.get("/items/{item_id}")
def get_item(item_id: str) -> dict[str, str]:
    return {"id": item_id}


@app.get("/boom")
def boom() -> None:
    raise KeyError("discount_code")


@app.get("/upstream/{mode}")
async def upstream(mode: str, request: Request) -> dict[str, int]:
    def handler(req: httpx.Request) -> httpx.Response:
        if mode == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(500 if mode == "error" else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resp = await call_upstream(client, log, "inventory", "GET", "http://inventory/x")
    return {"status": resp.status_code}


@pytest.fixture
def client():
    # Version/metrics are per-process globals. In production each service is its own
    # process; here several service modules share one, so pin the version per test.
    set_version("0.1.0")
    with TestClient(app) as c:
        yield c


def test_metrics_use_route_template_not_raw_path(client):
    client.get("/items/abc")
    client.get("/items/xyz")
    text = client.get("/metrics").text
    assert 'http_requests_total{method="GET",route="/items/{item_id}",status="200"}' in text
    assert "/items/abc" not in text
    assert 'app_build_info{version="0.1.0"} 1.0' in text


def test_crash_becomes_logged_500(client, caplog):
    with caplog.at_level(logging.ERROR):
        resp = client.get("/boom")
    assert resp.status_code == 500
    crash = next(r for r in caplog.records if r.getMessage() == "unhandled error")
    assert crash.exc_info[0] is KeyError


@pytest.mark.parametrize(
    ("mode", "status", "message"),
    [("ok", 200, None), ("error", 502, "upstream error"), ("timeout", 504, "upstream timeout")],
)
def test_call_upstream_maps_failures(client, caplog, mode, status, message):
    with caplog.at_level(logging.ERROR):
        resp = client.get(f"/upstream/{mode}")
    assert resp.status_code == status
    if message:
        record = next(r for r in caplog.records if r.getMessage() == message)
        assert record.fields["target"] == "inventory"


def test_json_formatter_includes_fields_and_version():
    record = logging.LogRecord("svc", logging.WARNING, __file__, 1, "slow cache response", (), None)
    record.fields = fields(duration_ms=1200.5)["fields"]
    line = json.loads(JsonFormatter("inventory").format(record))
    assert line["service"] == "inventory"
    assert line["level"] == "warning"
    assert line["msg"] == "slow cache response"
    assert line["duration_ms"] == 1200.5
    assert "version" in line and "ts" in line
