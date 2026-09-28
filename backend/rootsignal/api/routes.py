from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from rootsignal import __version__
from rootsignal.api.schemas import HealthOut, InvestigationOut
from rootsignal.core.models import Alert
from rootsignal.core.state_machine import InvestigationState
from rootsignal.db.session import get_session
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
    return HealthOut(
        status="ok" if db_status == "ok" else "degraded",
        version=__version__,
        llm_provider=request.app.state.settings.llm_provider,
        database=db_status,
    )


@router.post(
    "/investigations",
    response_model=InvestigationOut,
    status_code=status.HTTP_201_CREATED,
    tags=["investigations"],
)
async def create_investigation(alert: Alert, session: SessionDep) -> InvestigationOut:
    row = await svc.create_investigation(session, alert)
    return InvestigationOut.model_validate(row)


@router.get("/investigations", response_model=list[InvestigationOut], tags=["investigations"])
async def list_investigations(
    session: SessionDep,
    state: InvestigationState | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[InvestigationOut]:
    rows = await svc.list_investigations(session, state=state, limit=limit)
    return [InvestigationOut.model_validate(r) for r in rows]


@router.get(
    "/investigations/{investigation_id}", response_model=InvestigationOut, tags=["investigations"]
)
async def get_investigation(investigation_id: str, session: SessionDep) -> InvestigationOut:
    row = await svc.get_investigation(session, investigation_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return InvestigationOut.model_validate(row)
