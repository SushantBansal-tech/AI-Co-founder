from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request

from app.dashboard.schemas import (
    AttentionResponse,
    ChannelPerformanceResponse,
    DashboardOverview,
    PipelineBreakdownResponse,
    TrendResponse,
)


router = APIRouter(prefix="/dashboard", tags=["Sales dashboard"])


def _service(request: Request):
    service = getattr(request.app.state, "dashboard_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dashboard service is not initialized.")
    return service


def _period(
    request: Request,
    date_from: date | None,
    date_to: date | None,
) -> tuple[date, date]:
    if date_from is None or date_to is None:
        default_from, default_to = _service(request).default_period()
        date_from = date_from or default_from
        date_to = date_to or default_to
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to.")
    if (date_to - date_from).days > 366:
        raise HTTPException(status_code=422, detail="Dashboard date range cannot exceed 367 days.")
    return date_from, date_to


@router.get("/overview", response_model=DashboardOverview)
async def overview(
    request: Request,
    business_id: str = Query(min_length=1, max_length=100),
    date_from: date | None = None,
    date_to: date | None = None,
    risk_after_days: int = Query(default=7, ge=0, le=365),
):
    date_from, date_to = _period(request, date_from, date_to)
    return await _service(request).overview(
        business_id=business_id,
        date_from=date_from,
        date_to=date_to,
        risk_after_days=risk_after_days,
    )


@router.get("/pipeline", response_model=PipelineBreakdownResponse)
async def pipeline(
    request: Request,
    business_id: str = Query(min_length=1, max_length=100),
):
    return await _service(request).pipeline(business_id=business_id)


@router.get("/attention", response_model=AttentionResponse)
async def attention(
    request: Request,
    business_id: str = Query(min_length=1, max_length=100),
    limit: int = Query(default=50, ge=1, le=200),
):
    return await _service(request).attention(
        business_id=business_id,
        limit=limit,
    )


@router.get("/trends", response_model=TrendResponse)
async def trends(
    request: Request,
    business_id: str = Query(min_length=1, max_length=100),
    date_from: date | None = None,
    date_to: date | None = None,
):
    date_from, date_to = _period(request, date_from, date_to)
    return await _service(request).trends(
        business_id=business_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/channels", response_model=ChannelPerformanceResponse)
async def channels(
    request: Request,
    business_id: str = Query(min_length=1, max_length=100),
    date_from: date | None = None,
    date_to: date | None = None,
):
    date_from, date_to = _period(request, date_from, date_to)
    return await _service(request).channels(
        business_id=business_id,
        date_from=date_from,
        date_to=date_to,
    )
