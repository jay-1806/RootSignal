from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from rootsignal.config import Settings
from rootsignal.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        llm_provider="mock",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
    )
    with TestClient(create_app(settings)) as c:
        yield c
