import fakeredis
from helpers import disable_fault, enable_fault

from demo.faults import FAULTS_KEY, FaultReader, Scenario


def make_reader(server: fakeredis.FakeServer, ttl: float) -> FaultReader:
    return FaultReader(fakeredis.FakeAsyncRedis(server=server, decode_responses=True), ttl)


async def test_reads_active_scenarios_and_ignores_unknown_keys(redis_server, redis):
    enable_fault(redis, Scenario.LATENCY_SPIKE)
    redis.hset(FAULTS_KEY, "not_a_scenario", "{}")
    reader = make_reader(redis_server, ttl=0)
    assert await reader.active() == {Scenario.LATENCY_SPIKE}
    assert await reader.is_active(Scenario.LATENCY_SPIKE)
    assert not await reader.is_active(Scenario.BAD_DEPLOY)


async def test_caches_until_ttl_expires(redis_server, redis):
    reader = make_reader(redis_server, ttl=60)
    assert await reader.active() == frozenset()
    enable_fault(redis, Scenario.BAD_DEPLOY)
    assert await reader.active() == frozenset()  # still cached

    fresh = make_reader(redis_server, ttl=0)
    assert await fresh.is_active(Scenario.BAD_DEPLOY)
    disable_fault(redis, Scenario.BAD_DEPLOY)
    assert not await fresh.is_active(Scenario.BAD_DEPLOY)
