import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rootsignal.db.models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_pre_ping=True)


def alembic_config(database_url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


def run_migrations(database_url: str) -> None:
    """alembic upgrade head. Sync, because Alembic's env.py runs its own event loop."""
    command.upgrade(alembic_config(database_url), "head")


async def prepare_database(engine: AsyncEngine, database_url: str, auto_migrate: bool) -> None:
    """SQLite (tests, quick local runs): create tables directly.
    Postgres: run Alembic migrations in a worker thread."""
    if engine.dialect.name == "sqlite":
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    elif auto_migrate:
        await asyncio.to_thread(run_migrations, database_url)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        yield session
