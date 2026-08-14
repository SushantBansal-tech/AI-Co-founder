import asyncio
import os
from datetime import timedelta
from uuid import uuid4
from types import SimpleNamespace

import pytest
from dotenv import load_dotenv
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.channels.outbound import MessageSendResult
from app.database import (
    BusinessDocument,
    ChannelSource,
    FollowUpJob,
    FollowUpJobStatus,
    FollowUpRecord,
    HandoffRecord,
    InventoryRecord,
    QuotationRecord,
    SalesOrder,
)
from app.followups.jobs import FollowUpJobService
from app.followups.service import utc_now
from app.graph2 import _mods, build_complete_graph, make_initial_state
from app.rag.models import AgentRAGContext, RetrievedChunk


load_dotenv()


@pytest.fixture(scope="session")
def event_loop_policy():
    if os.name == "nt":
        return asyncio.WindowsSelectorEventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


class _ConfirmedOutbound:
    async def send(self, **_kwargs):
        return MessageSendResult(
            provider_message_id=f"test-{uuid4()}",
            status="sent",
        )


class _DeterministicRAG:
    _DOCUMENTS = {
        "inventory_report.csv": (
            "product_code | product_name | warehouse | on_hand | reserved | "
            "available | reorder | safety | status | updated\n"
            "MSB-001 | MS Billet | Main | 500 | 0 | 500 | 100 | 50 | "
            "in_stock | 2026-07-30"
        ),
        "production_capacity.csv": (
            "product_code | product_name | plant | installed | committed | "
            "available_daily | unit | lead_days | status | updated\n"
            "MSB-001 | MS Billet | Plant-1 | 100 | 20 | 80 | MT | 7 | "
            "active | 2026-07-30"
        ),
        "delivery_zones.csv": (
            "zone_code | city | state | zone | min_qty | rate | transit_days | "
            "service | surcharge | status | updated\n"
            "LDH | Ludhiana | Punjab | North | 1 | 900 | 2 | road | 0 | "
            "active | 2026-07-30"
        ),
    }

    async def get_context(self, *, agent_name, state, top_k=5):
        return AgentRAGContext(
            agent_name=agent_name,
            query=f"integration query for {state.get('business_id')}",
            chunks=[],
        )


class _DeterministicStructuredData:
    async def catalog_product(self, _business_id, product_code):
        if product_code != "MSB-001":
            return None
        return SimpleNamespace(
            product_code="MSB-001", name="MS Billet",
            category="Steel Billet", grade="IS2062",
            specifications="100x100mm", unit="MT",
        )

    async def feasibility_indexes(self, _business_id):
        return (
            {"MSB-001": {
                "product_name": "MS Billet", "available_qty": 500.0,
                "warehouses": ["Main"], "last_updated": "2026-07-30",
            }},
            {"MSB-001": {
                "weekly_capacity_mt": 560.0, "lead_time_days": 7,
                "min_order_qty_mt": 1.0,
            }},
            {"ludhiana": {"zone": "North", "transit_days": 2}},
        )

    async def pricing_documents(self, _business_id):
        return {
            "prices": [], "costs": [], "transport": [],
            "discounts": [], "margins": [], "gst": [],
        }

    async def payment_term(self, *_args):
        return None

def _install_deterministic_agents(monkeypatch):
    modules = _mods()
    ia = modules["ia"]
    cat = modules["cat"]
    req = modules["req"]
    qual = modules["qual"]
    fe = modules["fe"]
    pe = modules["pe"]
    poe = modules["poe"]
    pov = modules["pov"]
    hb = modules["hb"]

    def extract_inquiry(raw, _client):
        return ia.InquiryExtraction(
            inquiry_id=raw.inquiry_id,
            customer_name="Postgres Test Buyer",
            company_name="Postgres Test Steel",
            contact_person="Test Buyer",
            customer_email=raw.sender_identifier,
            product_requested="MS Billet",
            quantity="100 MT",
            specifications="IS2062 100x100mm",
            delivery_location="Ludhiana",
            extraction_confidence=1.0,
            missing_fields=[],
        )

    def match_requirement(*, extraction, client, rag_context):
        product = cat.CatalogProduct(
            product_code="MSB-001",
            name="MS Billet",
            category="Steel Billet",
            grade="IS2062",
            specifications="100x100mm",
        )
        return req.RequirementSummary(
            inquiry_id=extraction.inquiry_id,
            match_type=req.MatchType.EXACT,
            matched_product=product,
            similarity_score=0.99,
            requires_human_review=False,
            summary_text="Exact deterministic integration-test match.",
        )

    def qualify_lead(inquiry_id, profile, client, **_kwargs):
        return qual.QualificationResult(
            inquiry_id=inquiry_id,
            company_name=profile.company_name,
            customer_type=profile.customer_type.value,
            score=85,
            score_breakdown={},
            temperature=qual.LeadTemperature.HOT,
            priority=qual.Priority.P1,
            credit_risk_flag=True,
            credit_risk_reason="Integration-test credit approval.",
            requires_human_review=True,
            human_review_reason="Integration-test credit approval.",
            rationale="Deterministic integration-test qualification.",
        )

    def check_feasibility(*, extraction, **_kwargs):
        return fe.FeasibilityResult(
            inquiry_id=extraction.inquiry_id,
            fulfillment_type=fe.FulfillmentType.FROM_STOCK,
            stock_qty=100,
            production_qty=0,
            transit_days=2,
            total_lead_time_days=2,
            delivery_location="Ludhiana",
            delivery_zone="North",
            location_found=True,
            requires_human_review=True,
            human_review_reasons=["Integration-test feasibility approval."],
            narrative="Stock and transport are available.",
        )

    def compute_pricing(*, extraction, **_kwargs):
        return pe.PricingResult(
            inquiry_id=extraction.inquiry_id,
            product_code="MSB-001",
            product_name="MS Billet",
            quantity_mt=100,
            rm_cost_per_mt=42_000,
            overhead_per_mt=2_000,
            transport_per_mt=1_000,
            total_cost_per_mt=45_000,
            list_price_per_mt=52_000,
            floor_price_per_mt=50_000,
            suggested_price_per_mt=52_000,
            discounted_price_per_mt=52_000,
            min_margin_pct=10,
            target_margin_pct=12,
            actual_margin_pct=13.46,
            gst_rate_pct=18,
            gst_per_mt=9_360,
            final_price_per_mt_ex_gst=52_000,
            final_price_per_mt_inc_gst=61_360,
            subtotal_ex_gst=5_200_000,
            gst_amount=936_000,
            total_invoice_value=6_136_000,
            requires_human_approval=True,
            approval_reasons=["Integration-test pricing approval."],
            can_proceed_without_approval=False,
            pricing_possible=True,
            price_logic={"validation": {"missing_inputs": []}},
            explanation="All mandatory pricing inputs are present.",
        )

    def extract_po_fields(po_text, _client):
        return poe.POExtraction(
            po_number=f"PG-PO-{uuid4().hex[:8]}",
            po_date="2026-07-30",
            buyer_company="Postgres Test Steel",
            product_description="MS Billet IS2062 100x100mm",
            product_code="MSB-001",
            quantity=100,
            unit="MT",
            price_per_unit_ex_gst=52_000,
            gst_rate_pct=18,
            gst_amount=936_000,
            total_amount_inc_gst=6_136_000,
            payment_terms="100% advance",
            delivery_date="2026-08-15",
            delivery_location="Ludhiana",
            extraction_confidence=1.0,
            missing_critical_fields=[],
            raw_text=po_text,
        )

    def validate_po(*, po, draft, rag_context):
        return pov.POValidationResult(
            verdict=pov.ValidationVerdict.VALID,
            mismatches=[],
            can_proceed=True,
            requires_customer_correction=False,
            requires_human_review=True,
            internal_note="Integration-test PO approval.",
            summary="PO matches the confirmed quotation.",
        )

    def build_all_packages(
        *,
        sales_order_id,
        po,
        quotation_number,
        **_kwargs,
    ):
        package = hb.HandoffPackage(
            department=hb.DepartmentType.PRODUCTION,
            priority="P1",
            job_reference=f"PG-JOB-{uuid4().hex[:8]}",
            subject="Confirmed PostgreSQL test order",
            summary="Prepare the confirmed test order.",
            action_items=["Reserve production capacity."],
            deadline=po.delivery_date,
            structured_data={"product_code": po.product_code},
        )
        return hb.HandoffSummary(
            sales_order_id=sales_order_id,
            po_number=po.po_number or "",
            quotation_number=quotation_number,
            buyer_company=po.buyer_company or "",
            total_value=po.total_amount_inc_gst or 0,
            packages=[package],
        )

    monkeypatch.setattr(ia, "extract_inquiry", extract_inquiry)
    monkeypatch.setattr(req, "match_requirement", match_requirement)
    monkeypatch.setattr(qual, "qualify_lead", qualify_lead)
    monkeypatch.setattr(fe, "check_feasibility", check_feasibility)
    monkeypatch.setattr(pe, "compute_pricing", compute_pricing)
    monkeypatch.setattr(poe, "extract_po_fields", extract_po_fields)
    monkeypatch.setattr(pov, "validate_po", validate_po)
    monkeypatch.setattr(hb, "build_all_packages", build_all_packages)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_postgres_inquiry_to_handoff(monkeypatch):
    if os.getenv("RUN_POSTGRES_E2E", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("Set RUN_POSTGRES_E2E=1 to run the real PostgreSQL workflow.")

    database_url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    checkpoint_url = (
        os.getenv("TEST_LANGGRAPH_DATABASE_URL")
        or os.getenv("LANGGRAPH_DATABASE_URL")
    )
    if not database_url or not database_url.startswith("postgresql+asyncpg://"):
        pytest.fail("TEST_DATABASE_URL must be a postgresql+asyncpg:// URL.")
    if not checkpoint_url or not checkpoint_url.startswith("postgresql://"):
        pytest.fail("TEST_LANGGRAPH_DATABASE_URL must be a postgresql:// URL.")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1

    _install_deterministic_agents(monkeypatch)
    thread_id = str(uuid4())
    business_id = f"postgres-e2e-{uuid4()}"
    sender = f"{uuid4().hex}@example.com"
    config = {"configurable": {"thread_id": thread_id}}

    async with session_factory() as session:
        source_document = BusinessDocument(
            business_id=business_id,
            logical_name="integration-inventory",
            original_filename="integration_inventory.csv",
            document_type="inventory_report",
            version="1.0",
            checksum_sha256=uuid4().hex + uuid4().hex,
            storage_path="integration://inventory",
            import_status="completed",
            row_count=1,
        )
        session.add(source_document)
        session.add(ChannelSource(
            business_id=business_id,
            channel="email",
            provider="integration-test",
            provider_account_id=sender,
            public_key=f"integration-email-{uuid4()}",
            name="Integration email source",
            active=True,
            configuration={},
        ))
        await session.flush()
        session.add(InventoryRecord(
            business_id=business_id,
            source_document_id=source_document.id,
            product_code="MSB-001",
            product_name="MS Billet",
            warehouse="Integration Warehouse",
            physical_qty=500,
            available_qty=500,
            reserved_qty=0,
        ))
        await session.commit()

    async with AsyncPostgresSaver.from_conn_string(checkpoint_url) as saver:
        await saver.setup()
        graph = build_complete_graph(
            session_factory=session_factory,
            rag_adapter=_DeterministicRAG(),
            client=object(),
            checkpointer=saver,
            outbound_dispatcher=_ConfirmedOutbound(),
            structured_data=_DeterministicStructuredData(),
        )

        state = make_initial_state(
            trigger="inquiry",
            business_id=business_id,
            thread_id=thread_id,
            source="email",
            raw_text="Need 100 MT MS Billet delivered to Ludhiana.",
            sender_identifier=sender,
            outbound_channel="email",
            outbound_recipient=sender,
        )
        result = await graph.ainvoke(state, config=config)
        assert result.get("error") is None
        assert result["pipeline_status"] == "awaiting_approval"
        assert result["waiting_for"] == "finance_manager"

        for stage, expected_status in (
            ("qualification", "awaiting_approval"),
            ("feasibility", "awaiting_approval"),
            ("pricing", "awaiting_customer_reply"),
        ):
            result = await graph.ainvoke(
                {
                    "business_id": business_id,
                    "thread_id": thread_id,
                    "trigger": "approved",
                    "approved_stage": stage,
                    "needs_human_approval": False,
                    "human_approval_stage": None,
                    "error": None,
                },
                config=config,
            )
            assert result.get("error") is None
            assert result["pipeline_status"] == expected_status

        async with session_factory() as session:
            scheduled_count = await session.scalar(
                select(func.count(FollowUpJob.id)).where(
                    FollowUpJob.business_id == business_id,
                    FollowUpJob.thread_id == thread_id,
                )
            )
            await session.execute(
                update(FollowUpJob)
                .where(
                    FollowUpJob.business_id == business_id,
                    FollowUpJob.thread_id == thread_id,
                    FollowUpJob.attempt_number == 1,
                )
                .values(
                        scheduled_for=(
                            utc_now() - timedelta(days=10_000)
                        ),
                        next_attempt_at=(
                            utc_now() - timedelta(days=10_000)
                        ),
                )
            )
            await session.commit()
        assert scheduled_count == 4

        workers = [
            FollowUpJobService(
                session_factory=session_factory,
                sales_graph=graph,
                worker_id=f"postgres-worker-{number}",
            )
            for number in (1, 2)
        ]
        processed = await asyncio.gather(
            *(worker.process_one() for worker in workers)
        )
        assert any(processed)

        async with session_factory() as session:
            sent_records = await session.scalar(
                select(func.count(FollowUpRecord.id)).where(
                    FollowUpRecord.business_id == business_id,
                    FollowUpRecord.quotation_id
                    == result["quotation_id"],
                    FollowUpRecord.attempt_number == 1,
                )
            )
            target_job_status = await session.scalar(
                select(FollowUpJob.status).where(
                    FollowUpJob.business_id == business_id,
                    FollowUpJob.thread_id == thread_id,
                    FollowUpJob.attempt_number == 1,
                )
            )
        assert sent_records == 1
        assert (
            target_job_status
            == FollowUpJobStatus.COMPLETED.value
        )

        result = await graph.ainvoke(
            {
                "business_id": business_id,
                "thread_id": thread_id,
                "trigger": "po_received",
                "po_raw_text": "PO for the confirmed quotation.",
                "error": None,
            },
            config=config,
        )
        assert result.get("error") is None
        assert result["pipeline_status"] == "awaiting_approval"

        result = await graph.ainvoke(
            {
                "business_id": business_id,
                "thread_id": thread_id,
                "trigger": "approved",
                "approved_stage": "po",
                "needs_human_approval": False,
                "human_approval_stage": None,
                "error": None,
            },
            config=config,
        )

    assert result.get("error") is None
    assert result["pipeline_status"] == "handed_off"
    assert result["order_won"] is True
    assert result["sales_order_id"]
    assert result["departments_notified"] == ["production"]

    async with session_factory() as session:
        quotation = await session.scalar(
            select(QuotationRecord).where(
                QuotationRecord.business_id == business_id
            )
        )
        sales_order = await session.scalar(
            select(SalesOrder).where(SalesOrder.business_id == business_id)
        )
        handoff = await session.scalar(
            select(HandoffRecord).where(
                HandoffRecord.business_id == business_id
            )
        )
        assert quotation is not None
        assert quotation.status.value == "sent"
        assert quotation.sent_at is not None
        assert sales_order is not None
        assert sales_order.customer_id == result["customer_id"]
        assert handoff is not None
        assert handoff.status == "sent"
        followup_statuses = (
            await session.execute(
                select(FollowUpJob.status)
                .where(
                    FollowUpJob.business_id == business_id,
                    FollowUpJob.thread_id == thread_id,
                )
                .order_by(FollowUpJob.attempt_number)
            )
        ).scalars().all()
        assert followup_statuses == [
            FollowUpJobStatus.COMPLETED.value,
            FollowUpJobStatus.CANCELLED.value,
            FollowUpJobStatus.CANCELLED.value,
            FollowUpJobStatus.CANCELLED.value,
        ]

    await engine.dispose()
