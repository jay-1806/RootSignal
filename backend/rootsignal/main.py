from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker

from rootsignal import __version__
from rootsignal.api import telemetry as telemetry_api
from rootsignal.api.routes import router
from rootsignal.config import Settings, get_settings
from rootsignal.db.session import create_tables, make_engine
from rootsignal.providers.llm import get_llm_provider
from rootsignal.providers.prometheus_loki import build_telemetry_provider
from rootsignal.providers.telemetry import TelemetryProvider


def create_app(
    settings: Settings | None = None, telemetry: TelemetryProvider | None = None
) -> FastAPI:
    """Build the app. Pass `telemetry` to swap in a fake provider (tests)."""
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        await create_tables(engine)
        app.state.engine = engine
        app.state.sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.telemetry = telemetry or build_telemetry_provider(settings)
        yield
        await app.state.telemetry.aclose()
        await engine.dispose()

    app = FastAPI(title="RootSignal", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.llm = get_llm_provider(settings)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    app.include_router(telemetry_api.router, prefix="/api")
    return app


app = create_app()
