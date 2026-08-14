from datetime import date, datetime, time, timedelta

from app.dashboard.schemas import (
    AttentionResponse,
    ChannelPerformanceResponse,
    DashboardOverview,
    DashboardPeriod,
    PipelineBreakdownResponse,
    TrendResponse,
)


class DashboardService:
    def __init__(self, repository) -> None:
        self.repository = repository

    @staticmethod
    def default_period() -> tuple[date, date]:
        today = date.today()
        return today - timedelta(days=29), today

    @staticmethod
    def boundaries(date_from: date, date_to: date) -> tuple[datetime, datetime]:
        return (
            datetime.combine(date_from, time.min),
            datetime.combine(date_to + timedelta(days=1), time.min),
        )

    async def overview(
        self,
        *,
        business_id: str,
        date_from: date,
        date_to: date,
        risk_after_days: int,
    ) -> DashboardOverview:
        start, end = self.boundaries(date_from, date_to)
        values = await self.repository.overview(
            business_id=business_id,
            start_naive=start,
            end_naive=end,
            risk_after_days=risk_after_days,
        )
        quotations = values["quotations_sent"]
        values["quotation_conversion_pct"] = round(
            values["orders_won"] / quotations * 100.0, 2
        ) if quotations else 0.0
        return DashboardOverview(
            period=DashboardPeriod(date_from=date_from, date_to=date_to),
            **values,
        )

    async def pipeline(self, *, business_id: str) -> PipelineBreakdownResponse:
        return PipelineBreakdownResponse(
            items=await self.repository.pipeline_breakdown(
                business_id=business_id
            )
        )

    async def attention(
        self,
        *,
        business_id: str,
        limit: int,
    ) -> AttentionResponse:
        return AttentionResponse(
            items=await self.repository.attention_items(
                business_id=business_id,
                limit=limit,
            )
        )

    async def trends(
        self,
        *,
        business_id: str,
        date_from: date,
        date_to: date,
    ) -> TrendResponse:
        start, end = self.boundaries(date_from, date_to)
        rows = await self.repository.trends(
            business_id=business_id,
            start_naive=start,
            end_naive=end,
        )
        days: dict[date, dict] = {}

        def entry(raw_day):
            parsed = raw_day if isinstance(raw_day, date) else date.fromisoformat(str(raw_day))
            return days.setdefault(
                parsed,
                {
                    "day": parsed,
                    "rfqs": 0,
                    "quotations": 0,
                    "orders": 0,
                    "quoted_revenue": 0.0,
                    "won_revenue": 0.0,
                },
            )

        for raw_day, count in rows["rfqs"]:
            entry(raw_day)["rfqs"] = int(count)
        for raw_day, count, revenue in rows["quotations"]:
            item = entry(raw_day)
            item["quotations"] = int(count)
            item["quoted_revenue"] = float(revenue or 0)
        for raw_day, count, revenue in rows["orders"]:
            item = entry(raw_day)
            item["orders"] = int(count)
            item["won_revenue"] = float(revenue or 0)

        return TrendResponse(
            period=DashboardPeriod(date_from=date_from, date_to=date_to),
            items=[days[key] for key in sorted(days)],
        )

    async def channels(
        self,
        *,
        business_id: str,
        date_from: date,
        date_to: date,
    ) -> ChannelPerformanceResponse:
        start, end = self.boundaries(date_from, date_to)
        rows = await self.repository.channels(
            business_id=business_id,
            start_naive=start,
            end_naive=end,
        )
        for row in rows:
            row["conversion_pct"] = round(
                row["orders_won"] / row["rfqs"] * 100.0, 2
            ) if row["rfqs"] else 0.0
        return ChannelPerformanceResponse(
            period=DashboardPeriod(date_from=date_from, date_to=date_to),
            items=rows,
        )
