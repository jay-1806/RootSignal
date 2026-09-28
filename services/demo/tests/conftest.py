import dataclasses
from collections.abc import Callable
from types import ModuleType

import fakeredis
import pytest


@pytest.fixture
def redis_server() -> fakeredis.FakeServer:
    return fakeredis.FakeServer()


@pytest.fixture
def redis(redis_server: fakeredis.FakeServer) -> fakeredis.FakeRedis:
    """Sync client on the same fake server the apps use, for setting faults from tests."""
    return fakeredis.FakeRedis(server=redis_server, decode_responses=True)


@pytest.fixture
def patch_service(
    monkeypatch: pytest.MonkeyPatch, redis_server: fakeredis.FakeServer
) -> Callable[..., None]:
    """Point a service module at fake Redis and fast fault polling (plus any overrides)."""

    def patch(module: ModuleType, **overrides: object) -> None:
        monkeypatch.setattr(
            module,
            "make_redis",
            lambda _url: fakeredis.FakeAsyncRedis(server=redis_server, decode_responses=True),
        )
        if hasattr(module, "settings"):
            new = dataclasses.replace(module.settings, fault_poll_seconds=0.05, **overrides)
            monkeypatch.setattr(module, "settings", new)

    return patch
