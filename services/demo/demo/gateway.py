"""API gateway: the public entry point. Routes product lookups to inventory and checkouts
to checkout. Upstream failures show up here as 502/504s (the 'blast radius')."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from demo.common import Settings, call_upstream, create_service_app

VERSION = "2.3.1"
settings = Settings.from_env("gateway")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.http = httpx.AsyncClient(timeout=3.0)
    yield
    await app.state.http.aclose()


app, log = create_service_app(settings, VERSION, lifespan)


class CheckoutRequest(BaseModel):
    sku: str
    qty: int = Field(default=1, ge=1, le=10)


@app.get("/products/{sku}")
async def get_product(sku: str, request: Request) -> JSONResponse:
    resp = await call_upstream(
        request.app.state.http, log, "inventory", "GET", f"{settings.inventory_url}/stock/{sku}"
    )
    return JSONResponse(resp.json(), status_code=resp.status_code)


@app.post("/checkout")
async def checkout(body: CheckoutRequest, request: Request) -> JSONResponse:
    resp = await call_upstream(
        request.app.state.http,
        log,
        "checkout",
        "POST",
        f"{settings.checkout_url}/orders",
        json=body.model_dump(),
    )
    return JSONResponse(resp.json(), status_code=resp.status_code)
