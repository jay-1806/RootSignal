"""Fault scenarios shared by fault-control (writes) and the demo services (read).

State lives in Redis so fault-control can flip a scenario on and every service picks it
up within a couple of seconds. Services never log the scenario name: RootSignal must
find the cause from symptoms, like in a real incident.
"""

import time
from dataclasses import dataclass
from enum import StrEnum

from redis.asyncio import Redis
from redis.exceptions import RedisError

FAULTS_KEY = "rootsignal:faults"  # hash: scenario -> JSON {"since": iso timestamp}
CHANGES_KEY = "rootsignal:changes"  # list of JSON change events, newest first
MAX_CHANGES = 200

CHECKOUT_GOOD_VERSION = "1.8.0"
CHECKOUT_BAD_VERSION = "1.9.0"


class Scenario(StrEnum):
    BAD_DEPLOY = "bad_deploy"
    DB_POOL_EXHAUSTION = "db_pool_exhaustion"
    LATENCY_SPIKE = "latency_spike"
    MEMORY_LEAK = "memory_leak"


@dataclass(frozen=True)
class ScenarioInfo:
    target: str
    description: str


SCENARIOS: dict[Scenario, ScenarioInfo] = {
    Scenario.BAD_DEPLOY: ScenarioInfo(
        "checkout",
        f"Deploys checkout {CHECKOUT_BAD_VERSION}; a regression crashes orders that have "
        "no discount code (~40% of orders).",
    ),
    Scenario.DB_POOL_EXHAUSTION: ScenarioInfo(
        "inventory",
        "Inventory leaks database connections until its pool runs out; requests time out.",
    ),
    Scenario.LATENCY_SPIKE: ScenarioInfo(
        "inventory",
        "Inventory's Redis cache becomes slow (0.8-2s per call). Latency rises, no errors.",
    ),
    Scenario.MEMORY_LEAK: ScenarioInfo(
        "checkout",
        "Checkout keeps ~256KB per order in memory until it is OOM-killed and restarted.",
    ),
}


class FaultReader:
    """Read-only view of active scenarios, cached so requests don't hit Redis every time."""

    def __init__(self, redis: Redis, ttl_seconds: float = 2.0):
        self._redis = redis
        self._ttl = ttl_seconds
        self._active: frozenset[Scenario] = frozenset()
        self._fetched_at = float("-inf")

    async def active(self) -> frozenset[Scenario]:
        now = time.monotonic()
        if now - self._fetched_at < self._ttl:
            return self._active
        try:
            keys = await self._redis.hkeys(FAULTS_KEY)
            self._active = frozenset(Scenario(k) for k in keys if k in Scenario)
        except RedisError:
            pass  # keep the last known state if Redis blips
        self._fetched_at = now
        return self._active

    async def is_active(self, scenario: Scenario) -> bool:
        return scenario in await self.active()
