"""Checkout service: prices an order, then reserves stock in inventory.

Faults:
- bad_deploy:  runs version 1.9.0, whose promotions code assumes every cart has a
               discount code -> KeyError -> HTTP 500 on ~40% of orders.
- memory_leak: an unbounded in-process "order cache" grows ~256KB per order until the
               container hits its memory limit and is OOM-killed.
"""

import logging
import random
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from demo.common import (
    Settings,
    call_upstream,
    create_service_app,
    current_version,
    fields,
    make_redis,
    rss_bytes,
    set_version,
    start_periodic,
)
from demo.faults import CHECKOUT_BAD_VERSION, CHECKOUT_GOOD_VERSION, FaultReader, Scenario

settings = Settings.from_env("checkout")

DISCOUNTS = {"SAVE10": 0.10, "SAVE20": 0.20}
DISCOUNT_CODE_RATE = 0.6  # share of carts that carry a discount code
LEAK_BYTES_PER_ORDER = 256 * 1024
MEMORY_WARN_BYTES = 150 * 1024 * 1024

# Bug for the memory_leak scenario: orders are cached "for fast lookups" and never evicted.
_order_cache: list[bytes] = []


def apply_promotions(cart: dict[str, Any], version: str) -> float:
    promo = cart.get("promo", {})
    if version == CHECKOUT_BAD_VERSION:
        # Regression shipped in 1.9.0: assumes every cart carries a discount code.
        return DISCOUNTS.get(promo["discount_code"], 0.0)
    return DISCOUNTS.get(promo.get("discount_code", ""), 0.0)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = make_redis(settings.redis_url)
    app.state.faults = FaultReader(app.state.redis, settings.fault_poll_seconds)
    app.state.http = httpx.AsyncClient(timeout=2.5)

    async def track_version() -> None:
        bad = await app.state.faults.is_active(Scenario.BAD_DEPLOY)
        version = CHECKOUT_BAD_VERSION if bad else CHECKOUT_GOOD_VERSION
        if version != current_version():
            set_version(version)
            log.info("service started", extra=fields(version=version))

    async def report_memory() -> None:
        rss = rss_bytes()
        level = logging.WARNING if rss > MEMORY_WARN_BYTES else logging.INFO
        log.log(
            level,
            "memory usage",
            extra=fields(rss_mb=round(rss / 1024 / 1024, 1), cached_orders=len(_order_cache)),
        )

    tasks = [
        start_periodic(settings.fault_poll_seconds, track_version),
        start_periodic(15, report_memory),
    ]
    yield
    for task in tasks:
        task.cancel()
    await app.state.http.aclose()
    await app.state.redis.aclose()


app, log = create_service_app(settings, CHECKOUT_GOOD_VERSION, lifespan)


class OrderRequest(BaseModel):
    sku: str
    qty: int = Field(default=1, ge=1, le=10)


@app.post("/orders", status_code=201)
async def create_order(body: OrderRequest, request: Request) -> Any:
    state = request.app.state

    if await state.faults.is_active(Scenario.MEMORY_LEAK):
        _order_cache.append(b"\x01" * LEAK_BYTES_PER_ORDER)

    has_code = random.random() < DISCOUNT_CODE_RATE
    cart = {
        "sku": body.sku,
        "qty": body.qty,
        "promo": {"discount_code": "SAVE10"} if has_code else {},
    }
    discount = apply_promotions(cart, current_version())

    resp = await call_upstream(
        state.http,
        log,
        "inventory",
        "POST",
        f"{settings.inventory_url}/reserve",
        json={"sku": body.sku, "qty": body.qty},
    )
    if resp.status_code != 200:  # e.g. 404 unknown SKU, 409 out of stock
        return JSONResponse(resp.json(), status_code=resp.status_code)

    order_id = uuid.uuid4().hex[:12]
    log.info("order created", extra=fields(order_id=order_id, sku=body.sku, qty=body.qty))
    return {"order_id": order_id, "sku": body.sku, "qty": body.qty, "discount": discount}
