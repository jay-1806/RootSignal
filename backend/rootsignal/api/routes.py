from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rootsignal import __version__
from rootsignal.api.schemas import HealthOut, InvestigationOut, ResolveIn
from rootsignal.core.models import RCA, Alert
from rootsignal.core.state_machine import InvalidTransitionError, InvestigationState
from rootsignal.db.session import get_session
from rootsignal.engine.checks import Observation
from rootsignal.engine.memory import symptom_signature
from rootsignal.services import investigations as svc

router = APIRouter()
SessionDep = Annotated[AsyncSession, Depends(get_session)]


@router.get("/health", response_model=HealthOut, tags=["system"])
async def health(request: Request, session: SessionDep) -> HealthOut:
    try:
        await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception:
        db_status = "unavailable"
    llm = request.app.state.llm
    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        version=__version__,
        llm_provider=llm.name if llm else "mock",
        reasoner=request.app.state.reasoner.name,
        database=db_status,
    )


@router.post(
    "/investigations",
    response_model=InvestigationOut,
    status_code=status.HTTP_201_CREATED,
    tags=["investigations"],
)
async def create_investigation(
    alert: Alert,
    request: Request,
    session: SessionDep,
    run: Annotated[bool, Query(description="Start investigating immediately")] = True,
) -> InvestigationOut:
    row = await svc.create_investigation(session, alert)
    if run and request.app.state.settings.autostart:
        request.app.state.runner.start(row.id)
    return InvestigationOut.from_row(row)


@router.get("/investigations", response_model=list[InvestigationOut], tags=["investigations"])
async def list_investigations(
    session: SessionDep,
    state: InvestigationState | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InvestigationOut]:
    rows = await svc.list_investigations(session, state=state, limit=limit)
    return [InvestigationOut.from_row(r) for r in rows]


async def _get_or_404(session: AsyncSession, investigation_id: str):
    row = await svc.get_investigation(session, investigation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return row


@router.get(
    "/investigations/{investigation_id}", response_model=InvestigationOut, tags=["investigations"]
)
async def get_investigation(investigation_id: str, session: SessionDep) -> InvestigationOut:
    return InvestigationOut.from_row(await _get_or_404(session, investigation_id))


@router.post(
    "/investigations/{investigation_id}/run",
    response_model=InvestigationOut,
    tags=["investigations"],
)
async def run_investigation(
    investigation_id: str, request: Request, session: SessionDep
) -> InvestigationOut:
    """Start an investigation that was created with run=false."""
    row = await _get_or_404(session, investigation_id)
    if row.state != InvestigationState.RECEIVED:
        raise HTTPException(status_code=409, detail=f"Investigation is already '{row.state}'")
    request.app.state.runner.start(row.id)
    return InvestigationOut.from_row(row)


@router.post(
    "/investigations/{investigation_id}/resolve",
    response_model=InvestigationOut,
    tags=["investigations"],
)
async def resolve_investigation(
    investigation_id: str, verdict: ResolveIn, request: Request, session: SessionDep
) -> InvestigationOut:
    """Close an investigation with a human verdict. Confirmed or corrected root causes are
    saved to incident memory. Any proposed fix is NOT executed (approval comes in Phase 4)."""
    row = await _get_or_404(session, investigation_id)
    if row.state not in (InvestigationState.CONCLUDED, InvestigationState.AWAITING_APPROVAL):
        raise HTTPException(status_code=409, detail=f"Cannot resolve from '{row.state}'")

    rca = RCA.model_validate(row.rca) if row.rca else None
    corrected = any(v is not None for v in (verdict.category, verdict.service, verdict.root_cause))
    remember = rca is not None and (verdict.correct or corrected)
    if not rca and corrected and verdict.category and verdict.root_cause:
        remember = True

    if remember:
        triage = [Observation.model_validate(o) for o in (row.context or {}).get("triage", [])]
        await request.app.state.memory.add(
            session,
            investigation_id=row.id,
            title=row.title,
            service=verdict.service or (rca.service if rca else row.service),
            category=(verdict.category or rca.category).value,
            root_cause=verdict.root_cause or rca.root_cause,
            resolution=verdict.resolution,
            signature=symptom_signature(row.title, row.service, triage),
        )
    if corrected:
        note = "resolved: RCA corrected by a human, saved to incident memory"
    elif verdict.correct and remember:
        note = "resolved: RCA confirmed by a human, saved to incident memory"
    else:
        note = "resolved: RCA marked incorrect (not saved to memory)"
    try:
        row = await svc.transition_investigation(session, row, InvestigationState.RESOLVED, note)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return InvestigationOut.from_row(row)
