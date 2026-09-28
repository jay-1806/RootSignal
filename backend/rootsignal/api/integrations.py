"""Alert webhooks, incident memory search, and an LLM connectivity test."""

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rootsignal.api.schemas import LLMTestOut, MemoryEntryOut, WebhookResult
from rootsignal.db.models import IncidentMemoryRow
from rootsignal.db.session import get_session
from rootsignal.providers.alertmanager import AlertmanagerSource
from rootsignal.providers.llm import LLMError
from rootsignal.services import investigations as svc

router = APIRouter()
SessionDep = Annotated[AsyncSession, Depends(get_session)]
alertmanager = AlertmanagerSource()


def check_webhook_token(request: Request, authorization: str | None) -> None:
    expected = request.app.state.settings.alert_webhook_token
    if not expected:
        return
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="invalid webhook token")


@router.post("/webhooks/alertmanager", response_model=WebhookResult, tags=["webhooks"])
async def alertmanager_webhook(
    payload: dict[str, Any],
    request: Request,
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
) -> WebhookResult:
    """Start one investigation per firing alert. Alerts that already have an open
    investigation (same alert name + service) are deduplicated."""
    check_webhook_token(request, authorization)
    created, deduplicated = [], []
    for alert in alertmanager.parse(payload):
        existing = await svc.find_active(session, alert.title, alert.service)
        if existing is not None:
            deduplicated.append(existing.id)
            continue
        row = await svc.create_investigation(session, alert)
        created.append(row.id)
        if request.app.state.settings.autostart:
            request.app.state.runner.start(row.id)
    return WebhookResult(created=created, deduplicated=deduplicated)


@router.get("/memory", response_model=list[MemoryEntryOut], tags=["memory"])
async def list_memory(
    session: SessionDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> list[MemoryEntryOut]:
    rows = await session.scalars(
        select(IncidentMemoryRow).order_by(IncidentMemoryRow.created_at.desc()).limit(limit)
    )
    return [
        MemoryEntryOut.model_validate(
            {**r.__dict__, "created_at": r.created_at.isoformat() if r.created_at else None}
        )
        for r in rows
    ]


@router.get("/memory/similar", response_model=list[MemoryEntryOut], tags=["memory"])
async def similar_incidents(
    request: Request,
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=2000)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> list[MemoryEntryOut]:
    hits = await request.app.state.memory.search(session, q, limit=limit, min_similarity=0.0)
    return [MemoryEntryOut.model_validate(h) for h in hits]


class _Ping(BaseModel):
    reply: str


@router.post("/llm/test", response_model=LLMTestOut, tags=["system"])
async def llm_test(request: Request) -> LLMTestOut:
    """One tiny structured call to check your API key and model name (uses a few tokens)."""
    llm = request.app.state.llm
    if llm is None:
        raise HTTPException(status_code=400, detail="LLM_PROVIDER=mock: no LLM is configured")
    try:
        result = await llm.generate_structured(
            'Reply with {"reply": "pong"}.', _Ping, "You are a connectivity test. Be brief."
        )
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    u = result.usage
    return LLMTestOut(
        provider=u.provider,
        model=u.model,
        reply=result.output.reply,
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        latency_ms=u.latency_ms,
        cost_usd=u.cost_usd,
    )
