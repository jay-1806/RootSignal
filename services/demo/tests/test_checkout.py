import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import disable_fault, enable_fault, wait_until

from demo import checkout
from demo.faults import CHECKOUT_BAD_VERSION, CHECKOUT_GOOD_VERSION, Scenario

ORDER = {"sku": "SKU-001", "qty": 1}


def fake_inventory(status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/reserve"
        return httpx.Response(status, json={"sku": "SKU-001", "reserved": 1, "remaining": 9})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def client(patch_service):
    patch_service(checkout)
    with TestClient(checkout.app) as c:
        c.app.state.http = fake_inventory()
        wait_until(lambda: c.get("/health").json()["version"] == CHECKOUT_GOOD_VERSION)
        yield c


def test_apply_promotions_regression_only_in_bad_version():
    no_code = {"promo": {}}
    assert checkout.apply_promotions(no_code, CHECKOUT_GOOD_VERSION) == 0.0
    assert checkout.apply_promotions({"promo": {"discount_code": "SAVE10"}}, "1.9.0") == 0.10
    with pytest.raises(KeyError):
        checkout.apply_promotions(no_code, CHECKOUT_BAD_VERSION)


def test_order_succeeds_normally(client):
    resp = client.post("/orders", json=ORDER)
    assert resp.status_code == 201
    assert resp.json()["sku"] == "SKU-001"


def test_bad_deploy_changes_version_and_breaks_orders(client, redis, monkeypatch, caplog):
    monkeypatch.setattr(checkout, "DISCOUNT_CODE_RATE", 0.0)  # every cart lacks a code
    enable_fault(redis, Scenario.BAD_DEPLOY)
    wait_until(lambda: client.get("/health").json()["version"] == CHECKOUT_BAD_VERSION)
    assert f'app_build_info{{version="{CHECKOUT_BAD_VERSION}"}} 1.0' in client.get("/metrics").text

    with caplog.at_level(logging.ERROR):
        assert client.post("/orders", json=ORDER).status_code == 500
    crash = next(r for r in caplog.records if r.getMessage() == "unhandled error")
    assert crash.exc_info[0] is KeyError

    disable_fault(redis, Scenario.BAD_DEPLOY)
    wait_until(lambda: client.get("/health").json()["version"] == CHECKOUT_GOOD_VERSION)
    assert client.post("/orders", json=ORDER).status_code == 201


def test_memory_leak_grows_order_cache(client, redis, monkeypatch):
    monkeypatch.setattr(checkout, "_order_cache", [])
    enable_fault(redis, Scenario.MEMORY_LEAK)
    wait_until(lambda: (client.post("/orders", json=ORDER), len(checkout._order_cache) > 0)[1])
    before = len(checkout._order_cache)
    for _ in range(3):
        client.post("/orders", json=ORDER)
    assert len(checkout._order_cache) == before + 3


def test_inventory_failure_becomes_502(client, caplog):
    client.app.state.http = fake_inventory(status=503)
    with caplog.at_level(logging.ERROR):
        assert client.post("/orders", json=ORDER).status_code == 502
    assert any(r.getMessage() == "upstream error" for r in caplog.records)
