"""Investigation persistence. The API, and later the orchestrator/CLI/MCP, all go through here."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsignal.core.models import Alert, StateChange
from rootsignal.core.state_machine import InvestigationState, assert_transition
from rootsignal.db.models import InvestigationRow


async def create_investigation(session: AsyncSession, alert: Alert) -> InvestigationRow:
    first = StateChange(
        from_state=None, to_state=InvestigationState.RECEIVED, note="alert received"
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
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def get_investigation(
    session: AsyncSession, investigation_id: str
) -> InvestigationRow | None:
    return await session.get(InvestigationRow, investigation_id)


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
