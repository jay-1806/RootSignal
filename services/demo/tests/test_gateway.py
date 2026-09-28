import httpx
import pytest
from fastapi.testclient import TestClient

from demo import gateway


def upstreams(checkout_status: int = 201, inventory_status: int = 200) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/orders":
            return httpx.Response(checkout_status, json={"order_id": "abc"})
        return httpx.Response(inventory_status, json={"sku": "SKU-001", "qty": 5})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def client():
    with TestClient(gateway.app) as c:
        yield c


def test_routes_to_inventory_and_checkout(client):
    client.app.state.http = upstreams()
    assert client.get("/products/SKU-001").json()["qty"] == 5
    resp = client.post("/checkout", json={"sku": "SKU-001", "qty": 1})
    assert resp.status_code == 201 and resp.json()["order_id"] == "abc"


def test_passes_through_client_errors(client):
    client.app.state.http = upstreams(inventory_status=404)
    assert client.get("/products/NOPE").status_code == 404


def test_upstream_5xx_becomes_502(client):
    client.app.state.http = upstreams(checkout_status=500)
    assert client.post("/checkout", json={"sku": "SKU-001"}).status_code == 502
