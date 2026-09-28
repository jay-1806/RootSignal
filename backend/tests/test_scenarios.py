"""End-to-end investigations of the four demo faults (plus a healthy system), offline.

These run the real orchestrator, check catalog, scoring and rule-based reasoner against
scripted telemetry. They are the engine's regression suite, and a first version of the
evaluation harness.
"""

from pathlib import Path

import pytest
from fakes import bad_deploy, db_pool_exhaustion, healthy, latency_spike, memory_leak
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from rootsignal.config import Settings
from rootsignal.core.models import RCA, Alert, Category, Severity
from rootsignal.core.state_machine import InvestigationState as S
from rootsignal.db.models import Base
from rootsignal.engine.memory import MemoryStore
from rootsignal.engine.orchestrator import Orchestrator
from rootsignal.engine.reasoners import RuleBasedReasoner
from rootsignal.providers.remediation import ActionType
from rootsignal.services import investigations as svc


@pytest.fixture
async def sessionmaker(tmp_path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'e.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def investigate(sessionmaker, scenario, title, service, reasoner=None, memory=None):
    telemetry, changes = scenario()
    orch = Orchestrator(
        sessionmaker,
        telemetry,
        changes,
        reasoner or RuleBasedReasoner(),
        memory or MemoryStore(),
        Settings(database_url="sqlite+aiosqlite://"),
        clock=lambda: telemetry.now,
    )
    async with sessionmaker() as session:
        row = await svc.create_investigation(
            session, Alert(title=title, service=service, severity=Severity.CRITICAL)
        )
    await orch.run(row.id)
    async with sessionmaker() as session:
        return await svc.get_investigation(session, row.id), telemetry


DEPLOY, DB, CACHE, MEM = (
    Category.DEPLOY_REGRESSION,
    Category.DB_CONNECTION_EXHAUSTION,
    Category.CACHE_DEGRADATION,
    Category.MEMORY_LEAK,
)
ROLLBACK, RESTART = ActionType.ROLLBACK_DEPLOY, ActionType.RESTART_SERVICE
ERR, LAT = "HighErrorRate", "HighLatencyP95"

CASES = [
    # scenario, alert, alerting service, expected (category, service), expected action
    (bad_deploy, ERR, "checkout", (DEPLOY, "checkout"), ROLLBACK),
    (bad_deploy, ERR, "gateway", (DEPLOY, "checkout"), ROLLBACK),
    (db_pool_exhaustion, ERR, "inventory", (DB, "inventory"), RESTART),
    (db_pool_exhaustion, ERR, "checkout", (DB, "inventory"), RESTART),
    (latency_spike, LAT, "inventory", (CACHE, "inventory"), None),
    (latency_spike, LAT, "gateway", (CACHE, "inventory"), None),
    (memory_leak, "HighMemoryUsage", "checkout", (MEM, "checkout"), RESTART),
]


@pytest.mark.parametrize(("scenario", "title", "service", "expected", "action"), CASES)
async def test_finds_root_cause(sessionmaker, scenario, title, service, expected, action):
    row, _ = await investigate(sessionmaker, scenario, title, service)
    rca = RCA.model_validate(row.rca)
    top = row.hypotheses[0]

    assert (rca.category, rca.service) == expected, [
        (h["category"], h["service"], h["confidence"]) for h in row.hypotheses[:4]
    ]
    assert rca.conclusive and rca.confidence >= 0.7
    assert rca.confidence > row.hypotheses[1]["confidence"] + 0.2  # clear winner
    assert top["evidence"] and all(0 <= e["weight"] <= 1 for e in top["evidence"])
    assert rca.evidence and all(e.supports for e in rca.evidence)
    if action is None:
        assert rca.recommended_action is None
        assert row.state == S.CONCLUDED
    else:
        assert rca.recommended_action.type == action
        assert rca.recommended_action.service == expected[1]
        assert row.state == S.AWAITING_APPROVAL


async def test_bad_deploy_rca_details(sessionmaker):
    row, _ = await investigate(sessionmaker, bad_deploy, "HighErrorRate", "gateway")
    rca = RCA.model_validate(row.rca)
    assert rca.triggering_change == "checkout 1.8.0 -> 1.9.0"
    assert rca.recommended_action.params == {"to_version": "1.8.0"}
    assert rca.started_at is not None
    assert {"gateway", "checkout"} <= set(rca.affected_services)
    assert rca.written_by == "rule_based"
    states = [h["to_state"] for h in row.history]
    assert states == [
        "received",
        "gathering_context",
        "hypothesizing",
        "planning",
        "querying",
        "scoring",
        "concluded",
        "awaiting_approval",
    ]


async def test_healthy_system_is_inconclusive_and_proposes_nothing(sessionmaker):
    row, _ = await investigate(sessionmaker, healthy, "HighErrorRate", "checkout")
    rca = RCA.model_validate(row.rca)
    assert not rca.conclusive
    assert rca.recommended_action is None
    assert row.state == S.CONCLUDED
    assert row.iterations == 2  # refined once before giving up
    assert "hypothesizing" in [h["to_state"] for h in row.history[6:]]


async def test_only_catalog_queries_are_run(sessionmaker):
    _, telemetry = await investigate(sessionmaker, db_pool_exhaustion, "HighErrorRate", "inventory")
    allowed = ("http_requests_total", "http_request_duration", "process_", "app_build_info",
               "db_pool_", "cache_request", "upstream_requests", '{service="')  # fmt: skip
    assert telemetry.queries
    assert all(any(a in q for a in allowed) for q in telemetry.queries)
    assert len(telemetry.queries) == len(set(telemetry.queries))  # each query runs once


async def test_confirmed_past_incident_adds_evidence(sessionmaker):
    memory = MemoryStore()
    first, _ = await investigate(sessionmaker, db_pool_exhaustion, "HighErrorRate", "inventory")
    async with sessionmaker() as session:
        await memory.add(
            session,
            title=first.title,
            service="inventory",
            category="db_connection_exhaustion",
            root_cause="inventory leaked DB connections on the reserve error path",
            signature=first.context["signature"],
        )
    second, _ = await investigate(
        sessionmaker, db_pool_exhaustion, "HighErrorRate", "inventory", memory=memory
    )
    assert second.context["similar_incidents"][0]["similarity"] > 0.9
    kinds = [e["kind"] for e in second.hypotheses[0]["evidence"]]
    assert "past_incident" in kinds
    assert second.hypotheses[0]["confidence"] > first.hypotheses[0]["confidence"]


async def test_engine_failure_marks_investigation_failed(sessionmaker):
    class Broken(RuleBasedReasoner):
        async def hypothesize(self, *a, **k):
            raise RuntimeError("boom")

    row, _ = await investigate(
        sessionmaker, healthy, "HighErrorRate", "checkout", reasoner=Broken()
    )
    assert row.state == S.FAILED
    assert "RuntimeError: boom" in row.history[-1]["note"]
