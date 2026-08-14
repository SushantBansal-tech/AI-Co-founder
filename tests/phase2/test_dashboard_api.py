from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.dashboard import DashboardRepository, DashboardService
from app.dashboard.router import router
from app.database import (
    Customer,
    InquirySource,
    Interaction,
    Lead,
    LeadStatus,
    PipelineInstance,
    QuotationRecord,
    QuotationStatus,
    SalesOrder,
)


async def seed_sales_flow(
    session_factory,
    *,
    business_id: str,
    created_at: datetime,
    source=InquirySource.WEBSITE,
    waiting=True,
):
    customer = Customer(
        id=str(uuid4()),
        business_id=business_id,
        company_name=f"Customer {business_id}",
    )
    thread_id = str(uuid4())
    inquiry_id = str(uuid4())
    lead = Lead(
        id=str(uuid4()),
        business_id=business_id,
        customer_id=customer.id,
        thread_id=thread_id,
        inquiry_id=inquiry_id,
        source=source,
        status=LeadStatus.NEW,
        raw_text="Need 100 MT steel",
        created_at=created_at,
        updated_at=created_at,
    )
    quotation = QuotationRecord(
        id=str(uuid4()),
        business_id=business_id,
        customer_id=customer.id,
        thread_id=thread_id,
        quotation_number=f"QT-{uuid4().hex[:8]}",
        inquiry_id=inquiry_id,
        status=QuotationStatus.SENT,
        buyer_company=customer.company_name,
        total_inc_gst=1_000_000,
        draft_json="{}",
        html_content="<p>Quotation</p>",
        sent_at=created_at + timedelta(minutes=30),
        created_at=created_at + timedelta(minutes=20),
        updated_at=created_at + timedelta(minutes=30),
    )
    pipeline = PipelineInstance(
        business_id=business_id,
        customer_id=customer.id,
        lead_id=lead.id,
        thread_id=thread_id,
        pipeline_status=(
            "awaiting_customer_reply" if waiting else "handed_off"
        ),
        business_milestone="quotation_sent",
        waiting_for="customer" if waiting else "none",
        status_reason="Waiting for reply" if waiting else None,
        current_node="dispatch_quotation",
        created_at=created_at.replace(tzinfo=timezone.utc),
        updated_at=(created_at - timedelta(days=10)).replace(
            tzinfo=timezone.utc
        ),
    )
    incoming = Interaction(
        business_id=business_id,
        customer_id=customer.id,
        lead_id=lead.id,
        thread_id=thread_id,
        direction="incoming",
        channel="website",
        message_type="inquiry",
        content="Need a quote",
        status="received",
        occurred_at=created_at,
    )
    outgoing = Interaction(
        business_id=business_id,
        customer_id=customer.id,
        lead_id=lead.id,
        thread_id=thread_id,
        direction="outgoing",
        channel="email",
        message_type="quotation",
        content="Quotation sent",
        status="sent",
        occurred_at=created_at + timedelta(minutes=30),
    )
    order = SalesOrder(
        business_id=business_id,
        customer_id=customer.id,
        thread_id=thread_id,
        inquiry_id=inquiry_id,
        quotation_id=quotation.id,
        po_id=str(uuid4()),
        po_number=f"PO-{uuid4().hex[:8]}",
        buyer_company=customer.company_name,
        total_value=900_000,
        status="confirmed",
        created_at=created_at + timedelta(hours=2),
    ) if not waiting else None

    async with session_factory() as session:
        session.add_all(
            [customer, lead, quotation, pipeline, incoming, outgoing]
            + ([order] if order else [])
        )
        await session.commit()
    return thread_id


@pytest.mark.asyncio
async def test_dashboard_overview_is_tenant_scoped_and_deterministic(
    test_session_factory,
):
    created_at = datetime(2026, 8, 5, 9, 0)
    await seed_sales_flow(
        test_session_factory,
        business_id="tenant-a",
        created_at=created_at,
        waiting=False,
    )
    await seed_sales_flow(
        test_session_factory,
        business_id="tenant-b",
        created_at=created_at,
        waiting=False,
    )
    service = DashboardService(DashboardRepository(test_session_factory))
    result = await service.overview(
        business_id="tenant-a",
        date_from=date(2026, 8, 5),
        date_to=date(2026, 8, 5),
        risk_after_days=7,
    )
    assert result.rfqs_received == 1
    assert result.quotations_sent == 1
    assert result.orders_won == 1
    assert result.quoted_revenue == 1_000_000
    assert result.won_revenue == 900_000
    assert result.average_response_minutes == 30.0
    assert result.quotation_conversion_pct == 100.0


@pytest.mark.asyncio
async def test_dashboard_attention_and_revenue_at_risk(
    test_session_factory,
):
    await seed_sales_flow(
        test_session_factory,
        business_id="tenant-risk",
        created_at=datetime(2026, 8, 5, 9, 0),
        waiting=True,
    )
    service = DashboardService(DashboardRepository(test_session_factory))
    overview = await service.overview(
        business_id="tenant-risk",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
        risk_after_days=7,
    )
    attention = await service.attention(
        business_id="tenant-risk",
        limit=10,
    )
    assert overview.revenue_at_risk == 1_000_000
    assert len(attention.items) == 1
    assert attention.items[0].waiting_for == "customer"
    assert attention.items[0].value == 1_000_000


@pytest.mark.asyncio
async def test_dashboard_api_empty_dates_and_invalid_range(
    test_session_factory,
):
    app = FastAPI()
    app.include_router(router)
    app.state.dashboard_service = DashboardService(
        DashboardRepository(test_session_factory)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        empty = await client.get(
            "/dashboard/overview",
            params={
                "business_id": "empty-tenant",
                "date_from": "2026-08-01",
                "date_to": "2026-08-31",
            },
        )
        invalid = await client.get(
            "/dashboard/overview",
            params={
                "business_id": "empty-tenant",
                "date_from": "2026-09-01",
                "date_to": "2026-08-01",
            },
        )
    assert empty.status_code == 200
    assert empty.json()["rfqs_received"] == 0
    assert empty.json()["average_response_minutes"] is None
    assert invalid.status_code == 422
    assert "date_from" in invalid.json()["detail"]


@pytest.mark.asyncio
async def test_dashboard_trends_channels_and_pipeline(
    test_session_factory,
):
    await seed_sales_flow(
        test_session_factory,
        business_id="tenant-series",
        created_at=datetime(2026, 8, 5, 9, 0),
        source=InquirySource.WEBSITE,
        waiting=False,
    )
    service = DashboardService(DashboardRepository(test_session_factory))
    trends = await service.trends(
        business_id="tenant-series",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )
    channels = await service.channels(
        business_id="tenant-series",
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 31),
    )
    pipeline = await service.pipeline(business_id="tenant-series")
    assert trends.items[0].rfqs == 1
    assert trends.items[0].quotations == 1
    assert trends.items[0].orders == 1
    assert channels.items[0].channel == "website"
    assert channels.items[0].conversion_pct == 100.0
    assert pipeline.items[0].pipeline_status == "handed_off"
