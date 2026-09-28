"""Fault control: turn failure scenarios on and off, and keep a log of changes.

This is the demo's control plane, not part of the "production" system being watched,
so it isn't instrumented. It also holds the ground truth (which fault is really active)
used later to score RootSignal's answers.

Change events (deploys, config changes) are what RootSignal's ChangeProvider reads to
answer "what changed before it broke?". Enabling bad_deploy records a real-looking
deploy; POST /changes lets you add unrelated changes as red herrings.
"""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Literal

from fastapi import FastAPI, Query, Request
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from demo.common import Settings, make_redis
from demo.faults import (
    CHANGES_KEY,
    CHECKOUT_BAD_VERSION,
    CHECKOUT_GOOD_VERSION,
    FAULTS_KEY,
    MAX_CHANGES,
    SCENARIOS,
    Scenario,
)

settings = Settings.from_env("fault-control")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.redis = make_redis(settings.redis_url)
    yield
    await app.state.redis.aclose()


app = FastAPI(
    title="RootSignal fault control",
    description="Inject failures into the demo services and record change events.",
    lifespan=lifespan,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


class ScenarioOut(BaseModel):
    name: Scenario
    target: str
    description: str
    active: bool
    since: datetime | None = None


class ChangeIn(BaseModel):
    # Same shape as rootsignal.providers.changes.Change, so the backend can read it directly.
    kind: Literal["deploy", "config", "commit", "feature_flag"] = "config"
    service: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=300)
    ref: str = ""


class ChangeOut(ChangeIn):
    at: datetime


async def record_change(redis: Redis, change: ChangeIn) -> ChangeOut:
    out = ChangeOut(**change.model_dump(), at=utcnow())
    await redis.lpush(CHANGES_KEY, out.model_dump_json())
    await redis.ltrim(CHANGES_KEY, 0, MAX_CHANGES - 1)
    return out


async def list_scenarios(redis: Redis) -> list[ScenarioOut]:
    active = await redis.hgetall(FAULTS_KEY)
    out = []
    for scenario, info in SCENARIOS.items():
        raw = active.get(scenario.value)
        out.append(
            ScenarioOut(
                name=scenario,
                target=info.target,
                description=info.description,
                active=raw is not None,
                since=json.loads(raw)["since"] if raw else None,
            )
        )
    return out


async def enable(redis: Redis, scenario: Scenario) -> None:
    added = await redis.hsetnx(
        FAULTS_KEY, scenario.value, json.dumps({"since": utcnow().isoformat()})
    )
    if added and scenario == Scenario.BAD_DEPLOY:
        await record_change(
            redis,
            ChangeIn(
                kind="deploy",
                service="checkout",
                summary=f"checkout {CHECKOUT_GOOD_VERSION} -> {CHECKOUT_BAD_VERSION}",
                ref=f"checkout:{CHECKOUT_BAD_VERSION}",
            ),
        )


async def disable(redis: Redis, scenario: Scenario) -> None:
    removed = await redis.hdel(FAULTS_KEY, scenario.value)
    if removed and scenario == Scenario.BAD_DEPLOY:
        await record_change(
            redis,
            ChangeIn(
                kind="deploy",
                service="checkout",
                summary=f"checkout {CHECKOUT_BAD_VERSION} -> {CHECKOUT_GOOD_VERSION} (rollback)",
                ref=f"checkout:{CHECKOUT_GOOD_VERSION}",
            ),
        )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "fault-control"}


@app.get("/scenarios", response_model=list[ScenarioOut])
async def get_scenarios(request: Request) -> list[ScenarioOut]:
    return await list_scenarios(request.app.state.redis)


@app.post("/scenarios/{scenario}/enable", response_model=list[ScenarioOut])
async def enable_scenario(scenario: Scenario, request: Request) -> list[ScenarioOut]:
    await enable(request.app.state.redis, scenario)
    return await list_scenarios(request.app.state.redis)


@app.post("/scenarios/{scenario}/disable", response_model=list[ScenarioOut])
async def disable_scenario(scenario: Scenario, request: Request) -> list[ScenarioOut]:
    await disable(request.app.state.redis, scenario)
    return await list_scenarios(request.app.state.redis)


@app.post("/reset", response_model=list[ScenarioOut])
async def reset(request: Request) -> list[ScenarioOut]:
    for scenario in Scenario:
        await disable(request.app.state.redis, scenario)
    return await list_scenarios(request.app.state.redis)


@app.get("/changes", response_model=list[ChangeOut])
async def get_changes(
    request: Request,
    service: str | None = None,
    since: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=MAX_CHANGES),
) -> list[ChangeOut]:
    raw = await request.app.state.redis.lrange(CHANGES_KEY, 0, MAX_CHANGES - 1)
    changes = [ChangeOut.model_validate_json(r) for r in raw]
    if service:
        changes = [c for c in changes if c.service == service]
    if since:
        changes = [c for c in changes if c.at >= since]
    return changes[:limit]


@app.post("/changes", response_model=ChangeOut, status_code=201)
async def post_change(change: ChangeIn, request: Request) -> ChangeOut:
    return await record_change(request.app.state.redis, change)
