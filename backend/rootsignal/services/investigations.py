"""Investigation persistence. The API, orchestrator, CLI and MCP all go through here."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsignal.core.models import Alert, StateChange
from rootsignal.core.state_machine import TERMINAL_STATES, InvestigationState, assert_transition
from rootsignal.db.models import InvestigationRow


async def create_investigation(session: AsyncSession, alert: Alert) -> InvestigationRow:
    first = StateChange(
        from_state=None,
        to_state=InvestigationState.RECEIVED,
        note=f"alert received from {alert.source}",
    )
    row = InvestigationRow(
        title=alert.title,
        service=alert.service,
        severity=alert.severity.value,
        source=alert.source,
        state=InvestigationState.RECEIVED.value,
        alert=alert.model_dump(mode="json"),
        history=[first.model_dump(mode="json")],
        hypotheses=[],
        iterations=0,
        reasoner="",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_investigation(
    session: AsyncSession, investigation_id: str
) -> InvestigationRow | None:
    return await session.get(InvestigationRow, investigation_id)


async def find_active(session: AsyncSession, title: str, service: str) -> InvestigationRow | None:
    """An open investigation for the same alert and service (used to deduplicate alerts)."""
    stmt = (
        select(InvestigationRow)
        .where(
            InvestigationRow.title == title,
            InvestigationRow.service == service,
            InvestigationRow.state.not_in([s.value for s in TERMINAL_STATES]),
        )
        .order_by(InvestigationRow.created_at.desc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def list_investigations(
    session: AsyncSession, state: InvestigationState | None = None, limit: int = 50
) -> list[InvestigationRow]:
    stmt = select(InvestigationRow).order_by(InvestigationRow.created_at.desc()).limit(limit)
    if state is not None:
        stmt = stmt.where(InvestigationRow.state == state.value)
    return list((await session.scalars(stmt)).all())


async def transition_investigation(
    session: AsyncSession, row: InvestigationRow, target: InvestigationState, note: str = ""
) -> InvestigationRow:
    """Move to `target` if the state machine allows it, and record the change.

    Pending changes to other columns on `row` are committed in the same transaction.
    Raises InvalidTransitionError otherwise.
    """
    current = InvestigationState(row.state)
    assert_transition(current, target)
    change = StateChange(from_state=current, to_state=target, note=note)
    row.state = target.value
    # Assign a new list so SQLAlchemy notices the JSON column changed.
    row.history = [*row.history, change.model_dump(mode="json")]
    await session.commit()
    await session.refresh(row)
    return row
