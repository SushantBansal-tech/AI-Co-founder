from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, select

from app.database import (
    Customer,
    Interaction,
    Lead,
    PipelineInstance,
    QuotationRecord,
    SalesOrder,
)


class DashboardRepository:
    """Tenant-scoped read models for the manager dashboard."""

    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    @staticmethod
    def _latest_quotation():
        return (
            select(
                QuotationRecord.business_id.label("business_id"),
                QuotationRecord.thread_id.label("thread_id"),
                QuotationRecord.quotation_number.label("quotation_number"),
                QuotationRecord.total_inc_gst.label("total_inc_gst"),
                func.row_number()
                .over(
                    partition_by=(
                        QuotationRecord.business_id,
                        QuotationRecord.thread_id,
                    ),
                    order_by=(
                        QuotationRecord.created_at.desc(),
                        QuotationRecord.id.desc(),
                    ),
                )
                .label("row_number"),
            )
            .subquery()
        )

    async def overview(
        self,
        *,
        business_id: str,
        start_naive: datetime,
        end_naive: datetime,
        risk_after_days: int,
    ) -> dict:
        async with self.session_factory() as session:
            rfqs = await session.scalar(
                select(func.count(Lead.id)).where(
                    Lead.business_id == business_id,
                    Lead.created_at >= start_naive,
                    Lead.created_at < end_naive,
                )
            )
            quotation_row = (
                await session.execute(
                    select(
                        func.count(QuotationRecord.id),
                        func.coalesce(
                            func.sum(QuotationRecord.total_inc_gst), 0.0
                        ),
                    ).where(
                        QuotationRecord.business_id == business_id,
                        QuotationRecord.sent_at.is_not(None),
                        QuotationRecord.sent_at >= start_naive,
                        QuotationRecord.sent_at < end_naive,
                    )
                )
            ).one()
            order_row = (
                await session.execute(
                    select(
                        func.count(SalesOrder.id),
                        func.coalesce(func.sum(SalesOrder.total_value), 0.0),
                    ).where(
                        SalesOrder.business_id == business_id,
                        SalesOrder.created_at >= start_naive,
                        SalesOrder.created_at < end_naive,
                    )
                )
            ).one()
            open_approvals = await session.scalar(
                select(func.count(PipelineInstance.id)).where(
                    PipelineInstance.business_id == business_id,
                    PipelineInstance.pipeline_status == "awaiting_approval",
                )
            )
            blocked = await session.scalar(
                select(func.count(PipelineInstance.id)).where(
                    PipelineInstance.business_id == business_id,
                    PipelineInstance.pipeline_status == "blocked",
                )
            )
            failed = await session.scalar(
                select(func.count(PipelineInstance.id)).where(
                    PipelineInstance.business_id == business_id,
                    PipelineInstance.pipeline_status == "failed",
                )
            )
            average_response_seconds = await self._average_response_seconds(
                session,
                business_id=business_id,
                start_naive=start_naive,
                end_naive=end_naive,
            )
            revenue_at_risk = await self._revenue_at_risk(
                session,
                business_id=business_id,
                risk_after_days=risk_after_days,
            )

        return {
            "rfqs_received": int(rfqs or 0),
            "quotations_sent": int(quotation_row[0] or 0),
            "quoted_revenue": float(quotation_row[1] or 0),
            "orders_won": int(order_row[0] or 0),
            "won_revenue": float(order_row[1] or 0),
            "open_approvals": int(open_approvals or 0),
            "blocked_pipelines": int(blocked or 0),
            "failed_pipelines": int(failed or 0),
            "average_response_minutes": (
                round(float(average_response_seconds) / 60.0, 2)
                if average_response_seconds is not None
                else None
            ),
            "revenue_at_risk": float(revenue_at_risk or 0),
        }

    async def _average_response_seconds(
        self,
        session,
        *,
        business_id: str,
        start_naive: datetime,
        end_naive: datetime,
    ) -> float | None:
        incoming = (
            select(
                Interaction.thread_id.label("thread_id"),
                func.min(Interaction.occurred_at).label("received_at"),
            )
            .where(
                Interaction.business_id == business_id,
                Interaction.direction == "incoming",
                Interaction.message_type == "inquiry",
                Interaction.thread_id.is_not(None),
                Interaction.occurred_at >= start_naive,
                Interaction.occurred_at < end_naive,
            )
            .group_by(Interaction.thread_id)
            .subquery()
        )
        outgoing = (
            select(
                Interaction.thread_id.label("thread_id"),
                func.min(Interaction.occurred_at).label("responded_at"),
            )
            .where(
                Interaction.business_id == business_id,
                Interaction.direction == "outgoing",
                Interaction.message_type.in_(("quotation", "inquiry_followup")),
                Interaction.thread_id.is_not(None),
            )
            .group_by(Interaction.thread_id)
            .subquery()
        )
        rows = (
            await session.execute(
                select(incoming.c.received_at, outgoing.c.responded_at)
                .join(outgoing, outgoing.c.thread_id == incoming.c.thread_id)
                .where(outgoing.c.responded_at >= incoming.c.received_at)
            )
        ).all()
        if not rows:
            return None
        return sum(
            (responded_at - received_at).total_seconds()
            for received_at, responded_at in rows
        ) / len(rows)

    async def _revenue_at_risk(
        self,
        session,
        *,
        business_id: str,
        risk_after_days: int,
    ) -> float:
        latest = self._latest_quotation()
        cutoff = datetime.now(timezone.utc) - timedelta(days=risk_after_days)
        result = await session.scalar(
            select(func.coalesce(func.sum(latest.c.total_inc_gst), 0.0))
            .select_from(PipelineInstance)
            .join(
                latest,
                and_(
                    latest.c.business_id == PipelineInstance.business_id,
                    latest.c.thread_id == PipelineInstance.thread_id,
                    latest.c.row_number == 1,
                ),
            )
            .where(
                PipelineInstance.business_id == business_id,
                PipelineInstance.pipeline_status.in_(
                    (
                        "awaiting_customer_reply",
                        "awaiting_purchase_order",
                        "awaiting_corrected_po",
                    )
                ),
                PipelineInstance.updated_at <= cutoff,
                ~select(SalesOrder.id)
                .where(
                    SalesOrder.business_id == PipelineInstance.business_id,
                    SalesOrder.thread_id == PipelineInstance.thread_id,
                )
                .exists(),
            )
        )
        return float(result or 0)

    async def pipeline_breakdown(self, *, business_id: str) -> list[dict]:
        latest = self._latest_quotation()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        PipelineInstance.pipeline_status,
                        PipelineInstance.waiting_for,
                        func.count(PipelineInstance.id),
                        func.coalesce(func.sum(latest.c.total_inc_gst), 0.0),
                    )
                    .outerjoin(
                        latest,
                        and_(
                            latest.c.business_id == PipelineInstance.business_id,
                            latest.c.thread_id == PipelineInstance.thread_id,
                            latest.c.row_number == 1,
                        ),
                    )
                    .where(PipelineInstance.business_id == business_id)
                    .group_by(
                        PipelineInstance.pipeline_status,
                        PipelineInstance.waiting_for,
                    )
                    .order_by(func.count(PipelineInstance.id).desc())
                )
            ).all()
        return [
            {
                "pipeline_status": status,
                "waiting_for": waiting_for,
                "count": int(count),
                "total_value": float(total_value or 0),
            }
            for status, waiting_for, count, total_value in rows
        ]

    async def attention_items(
        self,
        *,
        business_id: str,
        limit: int,
    ) -> list[dict]:
        latest = self._latest_quotation()
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        PipelineInstance.thread_id,
                        PipelineInstance.customer_id,
                        Customer.company_name,
                        latest.c.quotation_number,
                        PipelineInstance.pipeline_status,
                        PipelineInstance.waiting_for,
                        PipelineInstance.status_reason,
                        PipelineInstance.failure_category,
                        func.coalesce(latest.c.total_inc_gst, 0.0),
                        PipelineInstance.updated_at,
                    )
                    .outerjoin(
                        Customer,
                        and_(
                            Customer.id == PipelineInstance.customer_id,
                            Customer.business_id == PipelineInstance.business_id,
                        ),
                    )
                    .outerjoin(
                        latest,
                        and_(
                            latest.c.business_id == PipelineInstance.business_id,
                            latest.c.thread_id == PipelineInstance.thread_id,
                            latest.c.row_number == 1,
                        ),
                    )
                    .where(
                        PipelineInstance.business_id == business_id,
                        (
                            (PipelineInstance.waiting_for != "none")
                            | PipelineInstance.pipeline_status.in_(
                                ("blocked", "failed", "retry_scheduled")
                            )
                        ),
                    )
                    .order_by(PipelineInstance.updated_at.asc())
                    .limit(limit)
                )
            ).all()
        return [
            {
                "thread_id": row[0],
                "customer_id": row[1],
                "customer_name": row[2],
                "quotation_number": row[3],
                "pipeline_status": row[4],
                "waiting_for": row[5],
                "status_reason": row[6],
                "failure_category": row[7],
                "value": float(row[8] or 0),
                "waiting_since": row[9],
            }
            for row in rows
        ]

    async def trends(
        self,
        *,
        business_id: str,
        start_naive: datetime,
        end_naive: datetime,
    ) -> dict[str, list]:
        async with self.session_factory() as session:
            rfqs = (
                await session.execute(
                    select(func.date(Lead.created_at), func.count(Lead.id))
                    .where(
                        Lead.business_id == business_id,
                        Lead.created_at >= start_naive,
                        Lead.created_at < end_naive,
                    )
                    .group_by(func.date(Lead.created_at))
                )
            ).all()
            quotations = (
                await session.execute(
                    select(
                        func.date(QuotationRecord.sent_at),
                        func.count(QuotationRecord.id),
                        func.coalesce(func.sum(QuotationRecord.total_inc_gst), 0.0),
                    )
                    .where(
                        QuotationRecord.business_id == business_id,
                        QuotationRecord.sent_at.is_not(None),
                        QuotationRecord.sent_at >= start_naive,
                        QuotationRecord.sent_at < end_naive,
                    )
                    .group_by(func.date(QuotationRecord.sent_at))
                )
            ).all()
            orders = (
                await session.execute(
                    select(
                        func.date(SalesOrder.created_at),
                        func.count(SalesOrder.id),
                        func.coalesce(func.sum(SalesOrder.total_value), 0.0),
                    )
                    .where(
                        SalesOrder.business_id == business_id,
                        SalesOrder.created_at >= start_naive,
                        SalesOrder.created_at < end_naive,
                    )
                    .group_by(func.date(SalesOrder.created_at))
                )
            ).all()
        return {"rfqs": rfqs, "quotations": quotations, "orders": orders}

    async def channels(
        self,
        *,
        business_id: str,
        start_naive: datetime,
        end_naive: datetime,
    ) -> list[dict]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        Lead.source,
                        func.count(func.distinct(Lead.id)),
                        func.count(func.distinct(QuotationRecord.id)),
                        func.count(func.distinct(SalesOrder.id)),
                    )
                    .outerjoin(
                        QuotationRecord,
                        and_(
                            QuotationRecord.business_id == Lead.business_id,
                            QuotationRecord.thread_id == Lead.thread_id,
                            QuotationRecord.sent_at.is_not(None),
                        ),
                    )
                    .outerjoin(
                        SalesOrder,
                        and_(
                            SalesOrder.business_id == Lead.business_id,
                            SalesOrder.thread_id == Lead.thread_id,
                        ),
                    )
                    .where(
                        Lead.business_id == business_id,
                        Lead.created_at >= start_naive,
                        Lead.created_at < end_naive,
                    )
                    .group_by(Lead.source)
                    .order_by(func.count(func.distinct(Lead.id)).desc())
                )
            ).all()
        return [
            {
                "channel": getattr(channel, "value", channel),
                "rfqs": int(rfqs),
                "quotations_sent": int(quotations),
                "orders_won": int(orders),
            }
            for channel, rfqs, quotations, orders in rows
        ]
