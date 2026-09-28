"""Inventory service: stock lookups (Redis cache in front of Postgres) and reservations.

Faults:
- db_pool_exhaustion: some reservations leak a DB connection (held for 30s, like a
                      transaction left open on an error path). The pool runs dry and
                      requests fail with "timed out acquiring database connection".
- latency_spike:      the Redis cache becomes slow (0.8-2s per call). Latency rises and
                      "slow cache response" warnings appear, but nothing errors.
"""

import asyncio
import json
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from prometheus_client import Gauge, Histogram
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from demo.common import SKUS, Settings, create_service_app, fields, make_redis
from demo.faults import FaultReader, Scenario

VERSION = "3.0.4"
settings = Settings.from_env("inventory")

INITIAL_QTY = 10_000
CACHE_TTL_SECONDS = 5
SLOW_CACHE_MS = 200
POOL_ACQUIRE_TIMEOUT_SECONDS = 1.0
LEAK_PROBABILITY = 0.5
LEAK_HOLD_SECONDS = 30.0
SPIKE_DELAY_SECONDS = (0.8, 2.0)

CACHE_LATENCY = Histogram(
    "cache_request_duration_seconds",
    "Latency of Redis cache calls",
    ["op"],
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
)
DB_POOL_IN_USE = Gauge("db_pool_connections_in_use", "Database connections currently checked out")
DB_POOL_MAX = Gauge("db_pool_connections_max", "Maximum size of the database connection pool")


async def connect_db(url: str, size: int, attempts: int = 20) -> asyncpg.Pool:
    for attempt in range(1, attempts + 1):
        try:
            return await asyncpg.create_pool(url, min_size=1, max_size=size)
        except (OSError, asyncpg.PostgresError):
            if attempt == attempts:
                raise
            await asyncio.sleep(1)
    raise RuntimeError("unreachable")


async def seed(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS stock ("
            " sku TEXT PRIMARY KEY, name TEXT NOT NULL, qty INTEGER NOT NULL)"
        )
        await conn.executemany(
            "INSERT INTO stock (sku, name, qty) VALUES ($1, $2, $3)"
            " ON CONFLICT (sku) DO UPDATE SET qty = EXCLUDED.qty",
            [(sku, f"Product {sku[-3:]}", INITIAL_QTY) for sku in SKUS],
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    pool = await connect_db(settings.inventory_db_url, settings.db_pool_size)
    await seed(pool)
    app.state.pool = pool
    app.state.redis = make_redis(settings.redis_url)
    app.state.faults = FaultReader(app.state.redis, settings.fault_poll_seconds)
    app.state.leaks = set()

    DB_POOL_MAX.set(pool.get_max_size())
    DB_POOL_IN_USE.set_function(lambda: pool.get_size() - pool.get_idle_size())
    yield
    for task in list(app.state.leaks):
        task.cancel()
    pool.terminate()
    await app.state.redis.aclose()


app, log = create_service_app(settings, VERSION, lifespan)


# --------------------------------------------------------------------------- helpers


async def cache_call(state: Any, op: str, key: str, fn: Callable[[], Awaitable[Any]]) -> Any:
    """Run a Redis call, timing it. Cache errors degrade to a miss instead of failing."""
    start = time.perf_counter()
    if await state.faults.is_active(Scenario.LATENCY_SPIKE):
        await asyncio.sleep(random.uniform(*SPIKE_DELAY_SECONDS))  # saturated Redis node
    try:
        result = await fn()
    except RedisError as exc:
        log.warning("cache unavailable", extra=fields(cache="redis", op=op, error=str(exc)))
        result = None
    elapsed = time.perf_counter() - start
    CACHE_LATENCY.labels(op=op).observe(elapsed)
    if elapsed * 1000 > SLOW_CACHE_MS:
        log.warning(
            "slow cache response",
            extra=fields(cache="redis", op=op, key=key, duration_ms=round(elapsed * 1000, 1)),
        )
    return result


@asynccontextmanager
async def db_connection(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    try:
        conn = await pool.acquire(timeout=POOL_ACQUIRE_TIMEOUT_SECONDS)
    except TimeoutError:
        log.error(
            "timed out acquiring database connection",
            extra=fields(
                pool_max=pool.get_max_size(),
                pool_in_use=pool.get_size() - pool.get_idle_size(),
                timeout_s=POOL_ACQUIRE_TIMEOUT_SECONDS,
            ),
        )
        raise HTTPException(status_code=503, detail="database unavailable") from None
    try:
        yield conn
    finally:
        await pool.release(conn)


def leak_connection(state: Any) -> None:
    """Grab a connection and sit on it, like a code path that forgets to release it."""

    async def hold() -> None:
        try:
            conn = await state.pool.acquire(timeout=0.1)
        except TimeoutError:
            return
        try:
            await asyncio.sleep(LEAK_HOLD_SECONDS)
        finally:
            await state.pool.release(conn)

    task = asyncio.create_task(hold())
    state.leaks.add(task)
    task.add_done_callback(state.leaks.discard)


def require_known_sku(sku: str) -> None:
    if sku not in SKUS:
        raise HTTPException(status_code=404, detail=f"unknown sku {sku}")


# --------------------------------------------------------------------------- routes


@app.get("/stock/{sku}")
async def get_stock(sku: str, request: Request) -> dict[str, Any]:
    require_known_sku(sku)
    state = request.app.state
    key = f"stock:{sku}"

    cached = await cache_call(state, "get", key, lambda: state.redis.get(key))
    if cached:
        return json.loads(cached)

    async with db_connection(state.pool) as conn:
        row = await conn.fetchrow("SELECT sku, name, qty FROM stock WHERE sku = $1", sku)
    item = dict(row)
    await cache_call(
        state, "set", key, lambda: state.redis.set(key, json.dumps(item), ex=CACHE_TTL_SECONDS)
    )
    return item


class ReserveRequest(BaseModel):
    sku: str
    qty: int = Field(default=1, ge=1, le=10)


@app.post("/reserve")
async def reserve(body: ReserveRequest, request: Request) -> dict[str, Any]:
    require_known_sku(body.sku)
    state = request.app.state

    if await state.faults.is_active(Scenario.DB_POOL_EXHAUSTION):
        if random.random() < LEAK_PROBABILITY:
            leak_connection(state)

    async with db_connection(state.pool) as conn:
        remaining = await conn.fetchval(
            "UPDATE stock SET qty = qty - $2 WHERE sku = $1 AND qty >= $2 RETURNING qty",
            body.sku,
            body.qty,
        )
    if remaining is None:
        raise HTTPException(status_code=409, detail="out of stock")

    key = f"stock:{body.sku}"
    await cache_call(state, "delete", key, lambda: state.redis.delete(key))
    return {"sku": body.sku, "reserved": body.qty, "remaining": remaining}
