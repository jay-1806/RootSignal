"""API flows with the engine running: autostart, Alertmanager webhook, resolve -> memory."""

import time
from pathlib import Path

import pytest
from conftest import sqlite_settings
from fakes import db_pool_exhaustion
from fastapi.testclient import TestClient

from rootsignal.engine.redact import redact
from rootsignal.main import create_app
from rootsignal.providers.alertmanager import AlertmanagerSource

AM_PAYLOAD = {
    "version": "4",
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "HighErrorRate",
                "service": "inventory",
                "severity": "critical",
            },
            "annotations": {"summary": "inventory: 30% of requests are failing"},
            "startsAt": "2026-09-28T10:00:00Z",
        },
        {"status": "resolved", "labels": {"alertname": "Old", "service": "gateway"}},
    ],
}


def make(tmp_path: Path, **settings) -> TestClient:
    telemetry, changes = db_pool_exhaustion()
    app = create_app(sqlite_settings(tmp_path, **settings), telemetry=telemetry, changes=changes)
    return TestClient(app)


def wait_for(client: TestClient, inv_id: str, states: set[str], timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        inv = client.get(f"/api/investigations/{inv_id}").json()
        if inv["state"] in states:
            return inv
        time.sleep(0.05)
    raise AssertionError(f"still {inv['state']}")


DONE = {"concluded", "awaiting_approval", "failed"}


def test_alertmanager_parsing():
    alerts = AlertmanagerSource().parse(AM_PAYLOAD)
    assert len(alerts) == 1
    a = alerts[0]
    assert (a.title, a.service, a.severity, a.source) == (
        "HighErrorRate",
        "inventory",
        "critical",
        "alertmanager",
    )
    assert a.description.startswith("inventory: 30%")


def test_webhook_starts_investigation_and_deduplicates(tmp_path):
    with make(tmp_path) as c:
        first = c.post("/api/webhooks/alertmanager", json=AM_PAYLOAD).json()
        assert len(first["created"]) == 1 and first["deduplicated"] == []
        again = c.post("/api/webhooks/alertmanager", json=AM_PAYLOAD).json()
        assert again == {"created": [], "deduplicated": first["created"]}

        inv = wait_for(c, first["created"][0], DONE)
        assert inv["state"] == "awaiting_approval"
        assert inv["rca"]["category"] == "db_connection_exhaustion"
        assert inv["rca"]["recommended_action"]["type"] == "restart_service"
        assert inv["llm_usage"]["calls"] == 0  # mock mode: no LLM calls


def test_webhook_token(tmp_path):
    with make(tmp_path, alert_webhook_token="s3cret") as c:
        assert c.post("/api/webhooks/alertmanager", json=AM_PAYLOAD).status_code == 401
        ok = c.post(
            "/api/webhooks/alertmanager",
            json=AM_PAYLOAD,
            headers={"Authorization": "Bearer s3cret"},
        )
        assert ok.status_code == 200


def test_resolve_saves_memory_and_next_investigation_uses_it(tmp_path):
    alert = {"title": "HighErrorRate", "service": "inventory", "severity": "critical"}
    with make(tmp_path) as c:
        first = wait_for(c, c.post("/api/investigations", json=alert).json()["id"], DONE)
        assert c.post(f"/api/investigations/{first['id']}/run").status_code == 409

        resolved = c.post(
            f"/api/investigations/{first['id']}/resolve",
            json={"correct": True, "resolution": "restarted inventory, fixed leak"},
        ).json()
        assert resolved["state"] == "resolved"
        assert "saved to incident memory" in resolved["history"][-1]["note"]
        assert c.post(f"/api/investigations/{first['id']}/resolve", json={}).status_code == 409

        memory = c.get("/api/memory").json()
        assert len(memory) == 1 and memory[0]["category"] == "db_connection_exhaustion"
        hits = c.get("/api/memory/similar", params={"q": first["context"]["signature"]}).json()
        assert hits[0]["similarity"] > 0.9

        second = wait_for(c, c.post("/api/investigations", json=alert).json()["id"], DONE)
        assert second["context"]["similar_incidents"][0]["id"] == memory[0]["id"]
        assert "past_incident" in [e["kind"] for e in second["hypotheses"][0]["evidence"]]


def test_incorrect_verdict_is_not_remembered(tmp_path):
    alert = {"title": "HighErrorRate", "service": "inventory"}
    with make(tmp_path) as c:
        inv = wait_for(c, c.post("/api/investigations", json=alert).json()["id"], DONE)
        c.post(f"/api/investigations/{inv['id']}/resolve", json={"correct": False})
        assert c.get("/api/memory").json() == []


def test_run_false_waits_for_explicit_start(tmp_path):
    with make(tmp_path) as c:
        inv = c.post(
            "/api/investigations?run=false", json={"title": "x", "service": "inventory"}
        ).json()
        time.sleep(0.2)
        assert c.get(f"/api/investigations/{inv['id']}").json()["state"] == "received"
        c.post(f"/api/investigations/{inv['id']}/run")
        assert wait_for(c, inv["id"], DONE)["state"] == "awaiting_approval"


def test_llm_test_endpoint_in_mock_mode(tmp_path):
    with make(tmp_path) as c:
        assert c.post("/api/llm/test").status_code == 400
        health = c.get("/api/health").json()
        assert health["llm_provider"] == "mock" and health["reasoner"] == "rule_based"


@pytest.mark.parametrize(
    ("text", "leaked"),
    [
        ("api_key=sk-abcdef123456 failed", "sk-abcdef123456"),
        ("user bob@example.com at 10.1.2.3", "bob@example.com"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9abcdefghijklmnop", "eyJhbGci"),
    ],
)
def test_redaction(text, leaked):
    assert leaked not in redact(text)
