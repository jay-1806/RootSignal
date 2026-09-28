from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rootsignal.config import Settings
from rootsignal.main import create_app
from rootsignal.providers.telemetry import (
    LogLine,
    MetricPoint,
    MetricSeries,
    TelemetryError,
    TelemetryProvider,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


class FakeTelemetry(TelemetryProvider):
    name = "fake"

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[tuple] = []

    def _maybe_fail(self) -> None:
        if self.fail:
            raise TelemetryError("prometheus is down")

    async def query_metrics(self, query, start, end, step_seconds=30):
        self._maybe_fail()
        self.calls.append(("metrics", query, start, end, step_seconds))
        return [MetricSeries(labels={"service": "checkout"}, points=[MetricPoint(at=NOW, value=1)])]

    async def query_logs(self, query, start, end, limit=200):
        self._maybe_fail()
        self.calls.append(("logs", query, start, end, limit))
        return [LogLine(at=NOW, service="checkout", level="error", message="boom")]

    async def list_services(self):
        self._maybe_fail()
        return ["checkout", "gateway"]


def make_client(tmp_path: Path, telemetry: FakeTelemetry) -> TestClient:
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    return TestClient(create_app(settings, telemetry=telemetry))


@pytest.fixture
def fake() -> FakeTelemetry:
    return FakeTelemetry()


@pytest.fixture
def client(tmp_path: Path, fake: FakeTelemetry) -> Iterator[TestClient]:
    with make_client(tmp_path, fake) as c:
        yield c


def test_services(client):
    assert client.get("/api/telemetry/services").json() == ["checkout", "gateway"]


def test_metrics_passes_query_and_window(client, fake):
    resp = client.get("/api/telemetry/metrics", params={"query": "up", "minutes": 10, "step": 15})
    assert resp.status_code == 200
    assert resp.json()[0]["labels"] == {"service": "checkout"}
    _, query, start, end, step = fake.calls[0]
    assert query == "up" and step == 15
    assert (end - start).total_seconds() == 600


def test_logs(client, fake):
    resp = client.get("/api/telemetry/logs", params={"query": '{service="checkout"}', "limit": 5})
    assert resp.json()[0]["message"] == "boom"
    assert fake.calls[0][-1] == 5


def test_validation(client):
    assert client.get("/api/telemetry/metrics").status_code == 422  # query required
    assert (
        client.get("/api/telemetry/metrics", params={"query": "up", "minutes": 0}).status_code
        == 422
    )


def test_backend_failure_returns_502(tmp_path):
    with make_client(tmp_path, FakeTelemetry(fail=True)) as c:
        resp = c.get("/api/telemetry/services")
    assert resp.status_code == 502
    assert "prometheus is down" in resp.json()["detail"]
