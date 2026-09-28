import json
import time
from collections.abc import Callable

import fakeredis

from demo.faults import FAULTS_KEY, Scenario


def enable_fault(redis: fakeredis.FakeRedis, scenario: Scenario) -> None:
    redis.hset(FAULTS_KEY, scenario.value, json.dumps({"since": "test"}))


def disable_fault(redis: fakeredis.FakeRedis, scenario: Scenario) -> None:
    redis.hdel(FAULTS_KEY, scenario.value)


def wait_until(check: Callable[[], bool], timeout: float = 5.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(interval)
    raise AssertionError("condition not met in time")
