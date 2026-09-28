import pytest

from rootsignal.core.state_machine import InvalidTransitionError, InvestigationState
from rootsignal.services import investigations as svc

ALERT = {"title": "High p99 latency on checkout", "service": "checkout", "severity": "critical"}


def test_health(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["llm_provider"] == "mock"


def test_create_and_get_investigation(client):
    resp = client.post("/api/investigations", json=ALERT)
    assert resp.status_code == 201
    inv = resp.json()
    assert inv["state"] == "received"
    assert inv["service"] == "checkout"
    assert len(inv["history"]) == 1
    assert inv["history"][0]["to_state"] == "received"

    fetched = client.get(f"/api/investigations/{inv['id']}").json()
    assert fetched["id"] == inv["id"]


def test_list_and_filter_by_state(client):
    client.post("/api/investigations", json=ALERT)
    client.post("/api/investigations", json={**ALERT, "title": "Errors on inventory"})
    assert len(client.get("/api/investigations").json()) == 2
    assert len(client.get("/api/investigations?state=received").json()) == 2
    assert client.get("/api/investigations?state=resolved").json() == []


def test_unknown_investigation_returns_404(client):
    assert client.get("/api/investigations/does-not-exist").status_code == 404


def test_invalid_alert_is_rejected(client):
    assert client.post("/api/investigations", json={"title": "", "service": "x"}).status_code == 422


def test_transitions_are_enforced_and_recorded(client):
    inv_id = client.post("/api/investigations", json=ALERT).json()["id"]
    sessionmaker = client.app.state.sessionmaker

    async def run():
        async with sessionmaker() as session:
            row = await svc.get_investigation(session, inv_id)
            await svc.transition_investigation(
                session, row, InvestigationState.GATHERING_CONTEXT, "collecting context"
            )
            with pytest.raises(InvalidTransitionError):
                await svc.transition_investigation(session, row, InvestigationState.RESOLVED)

    # TestClient runs the app on its own event loop; run DB work on that same loop.
    client.portal.call(run)

    inv = client.get(f"/api/investigations/{inv_id}").json()
    assert inv["state"] == "gathering_context"
    assert [h["to_state"] for h in inv["history"]] == ["received", "gathering_context"]
    assert inv["history"][1]["note"] == "collecting context"
