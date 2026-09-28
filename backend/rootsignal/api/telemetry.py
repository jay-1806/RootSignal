"""Read-only telemetry endpoints. Handy for debugging now; the CLI and MCP reuse them later."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from rootsignal.providers.telemetry import (
    LogLine,
    MetricSeries,
    TelemetryError,
    TelemetryProvider,
)

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

QueryParam = Annotated[str, Query(min_length=1, max_length=2000)]
MinutesParam = Annotated[int, Query(ge=1, le=360, description="Look-back window")]


def get_telemetry(request: Request) -> TelemetryProvider:
    return request.app.state.telemetry


TelemetryDep = Annotated[TelemetryProvider, Depends(get_telemetry)]


def _window(minutes: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    return end - timedelta(minutes=minutes), end


@router.get("/services", response_model=list[str])
async def list_services(telemetry: TelemetryDep) -> list[str]:
    try:
        return await telemetry.list_services()
    except TelemetryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/metrics", response_model=list[MetricSeries])
async def query_metrics(
    telemetry: TelemetryDep,
    query: QueryParam,
    minutes: MinutesParam = 15,
    step: Annotated[int, Query(ge=5, le=3600)] = 30,
) -> list[MetricSeries]:
    start, end = _window(minutes)
    try:
        return await telemetry.query_metrics(query, start, end, step_seconds=step)
    except TelemetryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/logs", response_model=list[LogLine])
async def query_logs(
    telemetry: TelemetryDep,
    query: QueryParam,
    minutes: MinutesParam = 15,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> list[LogLine]:
    start, end = _window(minutes)
    try:
        return await telemetry.query_logs(query, start, end, limit=limit)
    except TelemetryError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
