from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import async_sessionmaker

from rootsignal import __version__
from rootsignal.api import integrations as integrations_api
from rootsignal.api import telemetry as telemetry_api
from rootsignal.api.routes import router
from rootsignal.config import Settings, get_settings
from rootsignal.db.session import make_engine, prepare_database
from rootsignal.engine.memory import MemoryStore
from rootsignal.engine.orchestrator import InvestigationRunner, Orchestrator
from rootsignal.engine.reasoners import LLMReasoner, Reasoner, RuleBasedReasoner
from rootsignal.providers.changes import ChangeProvider
from rootsignal.providers.fault_control import FaultControlChangeProvider
from rootsignal.providers.llm import LLMProvider, get_llm_provider
from rootsignal.providers.prometheus_loki import build_telemetry_provider
from rootsignal.providers.telemetry import TelemetryProvider

_UNSET = object()


def create_app(
    settings: Settings | None = None,
    telemetry: TelemetryProvider | None = None,
    changes: ChangeProvider | None | object = _UNSET,
    llm: LLMProvider | None | object = _UNSET,
) -> FastAPI:
    """Build the app. Tests can inject fake telemetry, change and LLM providers."""
    settings = settings or get_settings()
    llm_http = httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
    llm_provider = get_llm_provider(settings, llm_http) if llm is _UNSET else llm
    reasoner: Reasoner = LLMReasoner(llm_provider) if llm_provider else RuleBasedReasoner()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.database_url)
        await prepare_database(engine, settings.database_url, settings.auto_migrate)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.engine = engine
        app.state.sessionmaker = sessionmaker
        app.state.telemetry = telemetry or build_telemetry_provider(settings)
        app.state.changes = (
            FaultControlChangeProvider(settings.changes_url) if changes is _UNSET else changes
        )
        app.state.memory = MemoryStore()
        orchestrator = Orchestrator(
            sessionmaker,
            app.state.telemetry,
            app.state.changes,
            reasoner,
            app.state.memory,
            settings,
        )
        app.state.runner = InvestigationRunner(orchestrator)
        await app.state.runner.recover(settings.autostart)
        yield
        await app.state.runner.shutdown()
        await app.state.telemetry.aclose()
        if app.state.changes is not None:
            await app.state.changes.aclose()
        await llm_http.aclose()
        await engine.dispose()

    app = FastAPI(title="RootSignal", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.llm = llm_provider
    app.state.reasoner = reasoner

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    app.include_router(telemetry_api.router, prefix="/api")
    app.include_router(integrations_api.router, prefix="/api")
    return app


app = create_app()
