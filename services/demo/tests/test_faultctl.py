import pytest
from fastapi.testclient import TestClient

from demo import faultctl


@pytest.fixture
def client(patch_service):
    patch_service(faultctl)
    with TestClient(faultctl.app) as c:
        yield c


def active(client) -> set[str]:
    return {s["name"] for s in client.get("/scenarios").json() if s["active"]}


def test_lists_all_scenarios_inactive(client):
    scenarios = client.get("/scenarios").json()
    assert {s["name"] for s in scenarios} == {
        "bad_deploy",
        "db_pool_exhaustion",
        "latency_spike",
        "memory_leak",
    }
    assert active(client) == set()


def test_enable_and_disable(client):
    client.post("/scenarios/latency_spike/enable")
    assert active(client) == {"latency_spike"}
    since = next(s for s in client.get("/scenarios").json() if s["active"])["since"]
    assert since is not None
    client.post("/scenarios/latency_spike/disable")
    assert active(client) == set()


def test_unknown_scenario_is_rejected(client):
    assert client.post("/scenarios/meteor_strike/enable").status_code == 422


def test_bad_deploy_records_deploy_and_rollback(client):
    client.post("/scenarios/bad_deploy/enable")
    client.post("/scenarios/bad_deploy/enable")  # idempotent: no duplicate deploy event
    client.post("/scenarios/bad_deploy/disable")

    changes = client.get("/changes").json()  # newest first
    assert [c["summary"] for c in changes] == [
        "checkout 1.9.0 -> 1.8.0 (rollback)",
        "checkout 1.8.0 -> 1.9.0",
    ]
    assert all(c["kind"] == "deploy" and c["service"] == "checkout" for c in changes)


def test_other_scenarios_record_no_change(client):
    client.post("/scenarios/memory_leak/enable")
    assert client.get("/changes").json() == []


def test_manual_changes_and_filters(client):
    resp = client.post(
        "/changes", json={"kind": "config", "service": "gateway", "summary": "raise timeout to 5s"}
    )
    assert resp.status_code == 201
    client.post("/scenarios/bad_deploy/enable")
    assert len(client.get("/changes").json()) == 2
    assert [c["service"] for c in client.get("/changes?service=gateway").json()] == ["gateway"]


def test_reset_disables_everything(client):
    for name in ("bad_deploy", "memory_leak"):
        client.post(f"/scenarios/{name}/enable")
    client.post("/reset")
    assert active(client) == set()
    assert client.get("/changes").json()[0]["summary"].endswith("(rollback)")
