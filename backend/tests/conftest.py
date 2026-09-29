from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rootsignal.config import Settings
from rootsignal.main import create_app


def sqlite_settings(tmp_path: Path, **overrides) -> Settings:
    return Settings(
        llm_provider="mock",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        **overrides,
    )


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """API client with autostart off: investigations stay in RECEIVED unless a test runs them."""
    with TestClient(create_app(sqlite_settings(tmp_path, autostart=False), changes=None)) as c:
        yield c
