"""Inventory against a real Postgres (the connection pool is the point of these tests).

Set INVENTORY_TEST_DB_URL to run them, e.g.
postgresql://inventory:inventory@localhost:5433/inventory. CI provides one.
"""

import logging
import os
import time

import pytest
from fastapi.testclient import TestClient
from helpers import disable_fault, enable_fault

from demo import inventory
from demo.faults import Scenario

DB_URL = os.getenv("INVENTORY_TEST_DB_URL")
pytestmark = pytest.mark.skipif(not DB_URL, reason="INVENTORY_TEST_DB_URL not set")


@pytest.fixture
def client(patch_service):
    patch_service(inventory, inventory_db_url=DB_URL, db_pool_size=2)
    with TestClient(inventory.app) as c:
        yield c


def reserve(client, qty=1):
    return client.post("/reserve", json={"sku": "SKU-001", "qty": qty})


def test_stock_lookup_and_unknown_sku(client):
    item = client.get("/stock/SKU-001").json()
    assert item == {"sku": "SKU-001", "name": "Product 001", "qty": inventory.INITIAL_QTY}
    assert client.get("/stock/SKU-001").json() == item  # served from cache
    assert client.get("/stock/NOPE").status_code == 404


def test_reserve_decrements_stock(client):
    assert reserve(client, qty=3).json()["remaining"] == inventory.INITIAL_QTY - 3
    assert client.get("/stock/SKU-001").json()["qty"] == inventory.INITIAL_QTY - 3


def test_latency_spike_slows_requests_and_logs_warning(client, redis, monkeypatch, caplog):
    monkeypatch.setattr(inventory, "SPIKE_DELAY_SECONDS", (0.25, 0.3))
    enable_fault(redis, Scenario.LATENCY_SPIKE)
    time.sleep(0.1)  # let the fault poll pick it up

    with caplog.at_level(logging.WARNING):
        start = time.perf_counter()
        resp = client.get("/stock/SKU-002")
        elapsed = time.perf_counter() - start
    assert resp.status_code == 200  # slow, but no errors
    assert elapsed >= 0.25
    slow = [r for r in caplog.records if r.getMessage() == "slow cache response"]
    assert slow and slow[0].fields["cache"] == "redis"


def test_db_pool_exhaustion_times_out_then_recovers(client, redis, monkeypatch, caplog):
    monkeypatch.setattr(inventory, "LEAK_PROBABILITY", 1.0)
    monkeypatch.setattr(inventory, "LEAK_HOLD_SECONDS", 1.5)
    monkeypatch.setattr(inventory, "POOL_ACQUIRE_TIMEOUT_SECONDS", 0.3)
    enable_fault(redis, Scenario.DB_POOL_EXHAUSTION)
    time.sleep(0.1)

    with caplog.at_level(logging.ERROR):
        statuses = [reserve(client).status_code for _ in range(4)]
    assert 503 in statuses
    timeout = next(
        r for r in caplog.records if r.getMessage() == "timed out acquiring database connection"
    )
    assert timeout.fields["pool_max"] == 2
    assert "db_pool_connections_in_use" in client.get("/metrics").text

    disable_fault(redis, Scenario.DB_POOL_EXHAUSTION)
    time.sleep(1.8)  # leaked connections are released
    assert reserve(client).status_code == 200
