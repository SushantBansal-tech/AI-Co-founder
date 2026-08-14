from datetime import date, datetime

from pydantic import BaseModel, Field


class DashboardPeriod(BaseModel):
    date_from: date
    date_to: date


class DashboardOverview(BaseModel):
    period: DashboardPeriod
    rfqs_received: int = 0
    quotations_sent: int = 0
    orders_won: int = 0
    quoted_revenue: float = 0.0
    won_revenue: float = 0.0
    revenue_at_risk: float = 0.0
    average_response_minutes: float | None = None
    quotation_conversion_pct: float = 0.0
    open_approvals: int = 0
    blocked_pipelines: int = 0
    failed_pipelines: int = 0


class PipelineStatusItem(BaseModel):
    pipeline_status: str
    waiting_for: str
    count: int
    total_value: float = 0.0


class PipelineBreakdownResponse(BaseModel):
    items: list[PipelineStatusItem] = Field(default_factory=list)


class AttentionItem(BaseModel):
    thread_id: str
    customer_id: str | None = None
    customer_name: str | None = None
    quotation_number: str | None = None
    pipeline_status: str
    waiting_for: str
    status_reason: str | None = None
    failure_category: str | None = None
    value: float = 0.0
    waiting_since: datetime


class AttentionResponse(BaseModel):
    items: list[AttentionItem] = Field(default_factory=list)


class TrendPoint(BaseModel):
    day: date
    rfqs: int = 0
    quotations: int = 0
    orders: int = 0
    quoted_revenue: float = 0.0
    won_revenue: float = 0.0


class TrendResponse(BaseModel):
    period: DashboardPeriod
    items: list[TrendPoint] = Field(default_factory=list)


class ChannelPerformanceItem(BaseModel):
    channel: str
    rfqs: int = 0
    quotations_sent: int = 0
    orders_won: int = 0
    conversion_pct: float = 0.0


class ChannelPerformanceResponse(BaseModel):
    period: DashboardPeriod
    items: list[ChannelPerformanceItem] = Field(default_factory=list)
