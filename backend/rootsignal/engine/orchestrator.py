"""Runs one investigation through the state machine, saving after every step.

    RECEIVED -> GATHERING_CONTEXT -> [HYPOTHESIZING -> PLANNING -> QUERYING -> SCORING] x N
             -> CONCLUDED -> (AWAITING_APPROVAL if a fix is recommended)

Code owns the control flow, the queries, the scoring and the fix recommendation. The
reasoner (LLM or rule-based) only proposes hypotheses and writes the narrative.
"""

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rootsignal.config import Settings
from rootsignal.core import topology
from rootsignal.core.models import (
    RCA,
    Alert,
    AlternativeHypothesis,
    Category,
    Hypothesis,
    utcnow,
)
from rootsignal.core.state_machine import InvestigationState as S
from rootsignal.db.models import InvestigationRow, LLMCallRow
from rootsignal.engine.checks import (
    CheckContext,
    CheckName,
    Observation,
    check_key,
    run_check,
    window,
)
from rootsignal.engine.memory import MemoryStore, symptom_signature
from rootsignal.engine.plan import PlanItem, merge, playbook
from rootsignal.engine.reasoners import InvestigationContext, LLMCallRecord, Reasoner
from rootsignal.engine.scoring import score
from rootsignal.providers.changes import ChangeProvider
from rootsignal.providers.remediation import ActionType, RemediationAction
from rootsignal.providers.telemetry import TelemetryProvider
from rootsignal.services import investigations as svc

log = logging.getLogger("rootsignal.engine")

TRIAGE_CHECKS = [
    CheckName.ERROR_RATE,
    CheckName.LATENCY_P95,
    CheckName.MEMORY,
    CheckName.RESTARTS,
    CheckName.VERSION_CHANGE,
    CheckName.UPSTREAM_ERRORS,
    CheckName.RECENT_CHANGES,
]
SYMPTOM_CHECKS = {CheckName.ERROR_RATE, CheckName.LATENCY_P95, CheckName.RESTARTS}
MAX_PARALLEL_QUERIES = 4
IN_PROGRESS = {
    S.GATHERING_CONTEXT,
    S.HYPOTHESIZING,
    S.PLANNING,
    S.QUERYING,
    S.SCORING,
    S.REMEDIATING,
}

Spec = tuple[CheckName, str, str | None]


def rank(hypotheses: list[Hypothesis]) -> list[Hypothesis]:
    """Highest confidence first. On ties, prefer the most downstream service (the likely origin)."""
    return sorted(hypotheses, key=lambda h: (-h.confidence, topology.depth(h.service), h.id))


def recommend(top: Hypothesis, conclusive: bool, observations: dict) -> RemediationAction | None:
    """Map the conclusion to an allowlisted action. Weak conclusions get no action."""
    if not conclusive:
        return None
    if top.category == Category.DEPLOY_REGRESSION:
        obs = observations.get(check_key(CheckName.VERSION_CHANGE, top.service, None))
        versions = obs.data.get("versions", []) if obs else []
        params = {"to_version": versions[-2]} if len(versions) >= 2 else {}
        return RemediationAction(
            type=ActionType.ROLLBACK_DEPLOY,
            service=top.service,
            params=params,
            reason="roll back the deploy that introduced the regression",
        )
    if top.category == Category.MEMORY_LEAK:
        return RemediationAction(
            type=ActionType.RESTART_SERVICE,
            service=top.service,
            reason="frees the leaked memory; the leak itself still needs a code fix",
        )
    if top.category == Category.DB_CONNECTION_EXHAUSTION:
        return RemediationAction(
            type=ActionType.RESTART_SERVICE,
            service=top.service,
            reason="releases leaked database connections; the leak still needs a code fix",
        )
    return None  # no safe automated fix: a human decides


class Orchestrator:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        telemetry: TelemetryProvider,
        changes: ChangeProvider | None,
        reasoner: Reasoner,
        memory: MemoryStore,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.sessionmaker = sessionmaker
        self.telemetry = telemetry
        self.changes = changes
        self.reasoner = reasoner
        self.memory = memory
        self.settings = settings
        self.clock = clock

    # ------------------------------------------------------------------ entry point

    async def run(self, investigation_id: str) -> None:
        async with self.sessionmaker() as session:
            row = await svc.get_investigation(session, investigation_id)
            if row is None or row.state != S.RECEIVED:
                return
            try:
                await self._investigate(session, row)
            except asyncio.CancelledError:
                await self._fail(session, row, "cancelled (backend shutting down)")
                raise
            except Exception as exc:  # the engine must never leave an investigation hanging
                log.exception("investigation %s failed", investigation_id)
                await self._fail(session, row, f"engine error: {type(exc).__name__}: {exc}")

    async def _fail(self, session: AsyncSession, row: InvestigationRow, note: str) -> None:
        await session.rollback()
        await session.refresh(row)
        if S(row.state) not in (S.RESOLVED, S.FAILED):
            await svc.transition_investigation(session, row, S.FAILED, note[:500])

    # ------------------------------------------------------------------ helpers

    async def _move(
        self, session: AsyncSession, row: InvestigationRow, to: S, note: str = ""
    ) -> None:
        await svc.transition_investigation(session, row, to, note)

    def _save_calls(
        self, session: AsyncSession, row: InvestigationRow, calls: list[LLMCallRecord]
    ) -> None:
        for rec in calls:
            session.add(LLMCallRow(investigation_id=row.id, **rec.model_dump()))
        calls.clear()

    async def _run_checks(
        self, ctx: CheckContext, specs: list[Spec], observations: dict[tuple, Observation]
    ) -> tuple[list[Observation], int]:
        """Run every spec not already observed (at most MAX_PARALLEL_QUERIES at once)."""
        todo = list(dict.fromkeys(s for s in specs if check_key(*s) not in observations))
        gate = asyncio.Semaphore(MAX_PARALLEL_QUERIES)

        async def one(spec: Spec) -> Observation:
            async with gate:
                return await run_check(ctx, *spec)

        for obs in await asyncio.gather(*(one(s) for s in todo)):
            observations[obs.key] = obs
        return [observations[check_key(*s)] for s in specs], len(todo)

    # ------------------------------------------------------------------ the investigation

    async def _investigate(self, session: AsyncSession, row: InvestigationRow) -> None:
        st = self.settings
        alert = Alert.model_validate(row.alert)
        start, end, since = window(self.clock(), st.lookback_minutes, st.changes_lookback_minutes)
        cctx = CheckContext(self.telemetry, self.changes, start, end, since)
        observations: dict[tuple, Observation] = {}
        calls: list[LLMCallRecord] = []

        # 1. Context: triage signals for the alerting service and everything it depends on.
        await self._move(
            session,
            row,
            S.GATHERING_CONTEXT,
            "collecting triage signals, changes, similar incidents",
        )
        if topology.is_known(alert.service):
            services = [alert.service, *topology.downstream(alert.service)]
        else:
            services = topology.known_services()
        triage, _ = await self._run_checks(
            cctx, [(c, s, None) for s in services for c in TRIAGE_CHECKS], observations
        )
        signature = symptom_signature(alert.title, alert.service, triage)
        similar = await self.memory.search(session, signature)
        ctx = InvestigationContext(
            alert=alert, services=services, triage=triage, similar_incidents=similar
        )
        row.context = {
            "window": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "changes_since": since.isoformat(),
            },
            "services": services,
            "triage": [o.model_dump(mode="json") for o in triage],
            "similar_incidents": similar,
            "signature": signature,
        }

        # 2. Hypothesize -> plan -> query -> score, refining until confident or out of rounds.
        hypotheses: list[Hypothesis] = []
        plan: list[PlanItem] = []
        sources: list[str] = []
        for iteration in range(1, st.max_iterations + 1):
            await self._move(session, row, S.HYPOTHESIZING, f"round {iteration}")
            drafts, source = await self.reasoner.hypothesize(ctx, hypotheses, iteration, calls)
            self._save_calls(session, row, calls)
            sources.append(source)

            new: list[Hypothesis] = []
            items: list[PlanItem] = []
            seen = {(h.category, h.service) for h in hypotheses}
            for d in drafts:
                if (d.category, d.service) in seen:
                    continue
                seen.add((d.category, d.service))
                h = Hypothesis(
                    id=f"h{len(hypotheses) + len(new) + 1}",
                    statement=d.statement,
                    category=d.category,
                    service=d.service,
                    rationale=d.rationale,
                    source=source,
                    iteration=iteration,
                )
                new.append(h)
                extra = [PlanItem(hypothesis_id=h.id, **c.model_dump()) for c in d.checks]
                items += merge(playbook(h.id, h.category, h.service), extra)
            plan += items
            row.hypotheses = [h.model_dump(mode="json") for h in rank(hypotheses) + new]

            await self._move(session, row, S.PLANNING, f"{len(new)} new hypotheses from {source}")
            await self._move(session, row, S.QUERYING, f"{len(items)} checks planned")
            _, ran = await self._run_checks(
                cctx, [(i.check, i.service, i.pattern) for i in items], observations
            )

            await self._move(session, row, S.SCORING, f"ran {ran} new queries")
            hypotheses += [score(h, plan, observations, similar) for h in new]
            ranked = rank(hypotheses)
            row.hypotheses = [h.model_dump(mode="json") for h in ranked]
            row.iterations = iteration
            if not ranked or ranked[0].confidence >= st.conclude_confidence:
                break

        # 3. Conclude.
        row.reasoner = ", ".join(dict.fromkeys(sources))
        ranked = rank(hypotheses)
        if not ranked:
            await self._move(session, row, S.CONCLUDED, "no hypotheses could be formed")
            return
        top, alternatives = ranked[0], ranked[1:4]
        narrative, writer = await self.reasoner.narrate(ctx, top, alternatives, calls)
        self._save_calls(session, row, calls)
        rca = self._build_rca(alert, top, alternatives, triage, observations, narrative, writer)
        row.rca = rca.model_dump(mode="json")
        await self._move(
            session,
            row,
            S.CONCLUDED,
            f"{'conclusive' if rca.conclusive else 'inconclusive'}: "
            f"{top.statement} ({top.confidence:.0%})",
        )
        if rca.recommended_action:
            a = rca.recommended_action
            await self._move(
                session,
                row,
                S.AWAITING_APPROVAL,
                f"proposed {a.type.value} on {a.service}; needs human approval",
            )

    def _build_rca(self, alert, top, alternatives, triage, observations, narrative, writer) -> RCA:
        conclusive = top.confidence >= self.settings.conclude_confidence
        supporting = [e for e in top.evidence if e.supports]
        starts = [e.started_at for e in supporting if e.started_at]
        affected = (
            [alert.service]
            + [
                o.service
                for o in triage
                if o.available and o.anomalous and o.check in SYMPTOM_CHECKS
            ]
            + [top.service]
        )

        change = None
        if top.category in (Category.DEPLOY_REGRESSION, Category.CONFIG_CHANGE):
            for e in supporting:
                if e.check == CheckName.RECENT_CHANGES and e.data.get("changes"):
                    change = e.data["changes"][-1].get("summary")
                    break
                if e.check == CheckName.VERSION_CHANGE and change is None:
                    change = e.summary

        return RCA(
            hypothesis_id=top.id,
            root_cause=top.statement,
            category=top.category,
            service=top.service,
            confidence=top.confidence,
            conclusive=conclusive,
            summary=narrative.summary,
            reasoning=narrative.reasoning,
            started_at=min(starts) if starts else None,
            affected_services=list(dict.fromkeys(affected)),
            triggering_change=change,
            evidence=supporting[:5],
            alternatives=[
                AlternativeHypothesis(statement=h.statement, confidence=h.confidence)
                for h in alternatives
            ],
            recommended_action=recommend(top, conclusive, observations),
            written_by=writer,
        )


class InvestigationRunner:
    """Runs investigations as background tasks inside the API process."""

    def __init__(self, orchestrator: Orchestrator):
        self.orchestrator = orchestrator
        self.tasks: set[asyncio.Task[None]] = set()

    def start(self, investigation_id: str) -> None:
        task = asyncio.create_task(self.orchestrator.run(investigation_id))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def recover(self, autostart: bool) -> None:
        """After a restart: fail investigations that were mid-flight, restart queued ones."""
        async with self.orchestrator.sessionmaker() as session:
            rows = (
                await session.scalars(
                    select(InvestigationRow).where(
                        InvestigationRow.state.in_([s.value for s in IN_PROGRESS | {S.RECEIVED}])
                    )
                )
            ).all()
            for row in rows:
                if row.state == S.RECEIVED:
                    if autostart:
                        self.start(row.id)
                else:
                    await svc.transition_investigation(
                        session, row, S.FAILED, "interrupted: backend restarted"
                    )

    async def shutdown(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
