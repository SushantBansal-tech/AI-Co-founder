import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("CRM_PASSWORD_ITERATIONS", "1000")

from app.authority.service import AuthorityService
from app.business_tools.executor import BusinessToolExecutor
from app.business_tools.router import router
from app.crm.auth import AuthenticatedUser, hash_password
from app.database import Base
from app.database.models.activity import BusinessEvent
from app.database.models.business_tool import AIToolExecution
from app.database.models.ai_action import AIActionRequest, ApprovalDecision
from app.database.models.authority import AuthorityApprovalRequest
from app.database.models.crm import BusinessMembership, CRMActivity, CRMTask, User
from app.database.models.customer import Customer
from app.database.models.lead import InquirySource, Lead, LeadStatus
from app.database.models.memory import CustomerNote
from app.database.models.followup_job import FollowUpJob
from app.database.models.pipeline import PipelineInstance
from app.database.models.quotation import QuotationRecord, QuotationStatus
from app.database.models.structured import (
    BusinessDocument,
    GstRateRecord,
    InventoryRecord,
    ProductCostRecord,
    ProductPriceRecord,
)


ALL_TOOL_SCOPES = [
    "customer:read", "customer_note:create", "lead:read", "pipeline:read",
    "approval:read", "inventory:read", "pricing_input:read", "task:read",
    "task:create", "activity:create", "followup:schedule", "quotation:prepare",
]


@pytest_asyncio.fixture
async def tool_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tools.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def seed_environment(factory, *, operating_mode="execute_low_risk"):
    async with factory() as session:
        founder = User(
            email="founder@example.com", normalized_email="founder@example.com",
            display_name="Founder", password_hash=hash_password("valid-password-123"),
        )
        session.add(founder)
        await session.flush()
        membership = BusinessMembership(
            business_id="tenant-a", user_id=founder.id, role="admin"
        )
        session.add(membership)
        customer = Customer(
            business_id="tenant-a", company_name="Visible Steel",
            contact_person="Buyer", email="buyer@visible.example",
        )
        hidden = Customer(
            business_id="tenant-b", company_name="Hidden Steel",
            email="hidden@example.com",
        )
        session.add_all([customer, hidden])
        await session.flush()
        lead = Lead(
            business_id="tenant-a", customer_id=customer.id,
            thread_id="tool-thread-1", inquiry_id="tool-inquiry-1",
            source=InquirySource.WEBSITE, status=LeadStatus.NEW,
            company_name=customer.company_name, product_requested="Steel Billet",
            quantity="100 MT", raw_text="Need 100 MT steel billet",
            assigned_to_user_id=founder.id,
        )
        session.add(lead)
        await session.flush()
        session.add(PipelineInstance(
            business_id="tenant-a", thread_id=lead.thread_id,
            customer_id=customer.id, lead_id=lead.id,
            pipeline_status="awaiting_approval", waiting_for="finance_manager",
            approval_stage="pricing", status_reason="Large quotation",
        ))
        document = BusinessDocument(
            business_id="tenant-a", logical_name="tool-master-data",
            original_filename="tool.csv", document_type="pricing_sheet",
            version="1", checksum_sha256="a" * 64, storage_path="tool.csv",
            import_status="completed", row_count=4,
        )
        session.add(document)
        await session.flush()
        session.add_all([
            InventoryRecord(
                business_id="tenant-a", source_document_id=document.id,
                product_code="MSB-001", product_name="Steel Billet",
                warehouse="Mumbai", physical_qty=Decimal("150"),
                reserved_qty=Decimal("20"), available_qty=Decimal("130"),
                damaged_qty=Decimal("0"), reorder_level=Decimal("30"),
                stock_status="available", last_updated=datetime.now(UTC).replace(tzinfo=None),
            ),
            ProductPriceRecord(
                business_id="tenant-a", source_document_id=document.id,
                product_code="MSB-001", product_name="Steel Billet", unit="MT",
                base_price_inr=Decimal("80000"), currency="INR",
                effective_from=date.today() - timedelta(days=1),
                effective_to=date.today() + timedelta(days=30),
                minimum_order_qty=Decimal("1"), status="active",
            ),
            ProductCostRecord(
                business_id="tenant-a", source_document_id=document.id,
                product_code="MSB-001", product_name="Steel Billet",
                rm_cost_per_mt=Decimal("62000"),
                manufacturing_overhead_pct=Decimal("5"),
            ),
            GstRateRecord(
                business_id="tenant-a", source_document_id=document.id,
                gst_rule_id="GST-001", product_code="MSB-001",
                product_category="billet", hsn_code="7207",
                gst_rate_pct=Decimal("18"), cgst_pct=Decimal("9"),
                sgst_pct=Decimal("9"), igst_pct=Decimal("18"),
                effective_from=date.today() - timedelta(days=1), status="active",
            ),
        ])
        await session.commit()

    owner = AuthenticatedUser(
        user_id=founder.id, business_id="tenant-a", membership_id=membership.id,
        role="admin", email=founder.email, display_name=founder.display_name,
    )
    authority = AuthorityService(factory)
    settings = await authority.get_settings(owner)
    if operating_mode != settings["ai_operating_mode"]:
        await authority.update_settings(owner, {
            "ai_operating_mode": operating_mode, "currency": "INR",
            "timezone": "Asia/Kolkata", "maximum_automatic_discount_pct": 3,
            "maximum_automatic_quotation_value": 5_000_000,
            "minimum_margin_pct": 12, "daily_outbound_message_limit": 100,
            "default_approval_role": "admin", "expected_version": 1,
            "change_reason": "Configure controlled tool tests",
        })
    principal = await authority.create_principal(
        owner, name="Jarvis Tools", scopes=ALL_TOOL_SCOPES
    )
    return {
        "founder": founder, "customer": customer, "hidden": hidden,
        "lead": lead, "authority": authority, "principal": principal,
    }


def application(factory, authority):
    app = FastAPI()
    app.state.session_factory = factory
    app.state.authority_service = authority
    app.state.business_tool_executor = BusinessToolExecutor(factory, authority)
    app.include_router(router)
    return app


def headers(principal, key=None):
    result = {"X-AI-Principal-Token": principal["credential"]}
    if key:
        result["Idempotency-Key"] = key
    return result


@pytest.mark.asyncio
async def test_catalog_contains_only_fixed_requested_tools(tool_factory):
    env = await seed_environment(tool_factory)
    app = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/ai/tools", headers=headers(env["principal"]))
    assert response.status_code == 200
    names = {item["name"] for item in response.json()["items"]}
    assert names == {
        "search_customers", "get_customer_360", "get_lead", "get_pipeline",
        "get_customer_sales_context",
        "get_pending_approvals", "get_inventory", "get_pricing_inputs",
        "get_open_tasks", "add_customer_note", "create_task",
        "record_activity", "schedule_followup", "prepare_quotation",
    }


@pytest.mark.asyncio
async def test_unknown_tool_and_invalid_arguments_are_rejected(tool_factory):
    env = await seed_environment(tool_factory)
    app = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        unknown = await client.post(
            "/ai/tools/run_sql/execute", headers=headers(env["principal"]),
            json={"arguments": {"sql": "SELECT 1"}},
        )
        invalid = await client.post(
            "/ai/tools/get_lead/execute", headers=headers(env["principal"]),
            json={"arguments": {}},
        )
    assert unknown.status_code == 404
    assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_read_tools_are_tenant_scoped(tool_factory):
    env = await seed_environment(tool_factory)
    app = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        visible = await client.post(
            "/ai/tools/search_customers/execute", headers=headers(env["principal"]),
            json={"arguments": {"search": "Steel"}},
        )
        hidden = await client.post(
            "/ai/tools/get_customer_360/execute", headers=headers(env["principal"]),
            json={"arguments": {"customer_id": env["hidden"].id}},
        )
        inventory = await client.post(
            "/ai/tools/get_inventory/execute", headers=headers(env["principal"]),
            json={"arguments": {"product_code": "MSB-001"}},
        )
        lead = await client.post(
            "/ai/tools/get_lead/execute", headers=headers(env["principal"]),
            json={"arguments": {"lead_id": env["lead"].id}},
        )
        pipeline = await client.post(
            "/ai/tools/get_pipeline/execute", headers=headers(env["principal"]),
            json={"arguments": {"thread_id": env["lead"].thread_id}},
        )
        approvals = await client.post(
            "/ai/tools/get_pending_approvals/execute", headers=headers(env["principal"]),
            json={"arguments": {}},
        )
        pricing = await client.post(
            "/ai/tools/get_pricing_inputs/execute", headers=headers(env["principal"]),
            json={"arguments": {"product_code": "MSB-001"}},
        )
        tasks = await client.post(
            "/ai/tools/get_open_tasks/execute", headers=headers(env["principal"]),
            json={"arguments": {}},
        )
    assert visible.status_code == 200
    assert [x["company_name"] for x in visible.json()["result"]["items"]] == ["Visible Steel"]
    assert hidden.status_code == 404
    assert Decimal(inventory.json()["result"]["total_available_quantity"]) == Decimal("130")
    assert lead.status_code == pipeline.status_code == approvals.status_code == 200
    assert pricing.status_code == tasks.status_code == 200
    assert approvals.json()["result"]["count"] == 1
    assert pricing.json()["result"]["prices"][0]["product_code"] == "MSB-001"


@pytest.mark.asyncio
async def test_missing_scope_is_rejected(tool_factory):
    env = await seed_environment(tool_factory)
    owner = AuthenticatedUser(
        user_id=env["founder"].id, business_id="tenant-a", membership_id="unused",
        role="admin", email=env["founder"].email,
        display_name=env["founder"].display_name,
    )
    limited = await env["authority"].create_principal(
        owner, name="Read Customers Only", scopes=["customer:read"]
    )
    app = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ai/tools/get_inventory/execute", headers=headers(limited),
            json={"arguments": {}},
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_duplicate_customer_note_creates_one_note_and_audit(tool_factory):
    env = await seed_environment(tool_factory)
    app = application(tool_factory, env["authority"])
    body = {"arguments": {
        "customer_id": env["customer"].id,
        "content_type": "preference",
        "content": "Customer prefers email quotations.",
    }}
    request_headers = headers(env["principal"], "note-idempotency-001")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first, duplicate = await asyncio.gather(
            client.post("/ai/tools/add_customer_note/execute", headers=request_headers, json=body),
            client.post("/ai/tools/add_customer_note/execute", headers=request_headers, json=body),
        )
        if 409 in {first.status_code, duplicate.status_code}:
            retry = await client.post(
                "/ai/tools/add_customer_note/execute", headers=request_headers, json=body
            )
            assert retry.status_code == 200
        else:
            assert first.status_code == duplicate.status_code == 200
    async with tool_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerNote)) == 1
        assert await session.scalar(select(func.count()).select_from(BusinessEvent).where(
            BusinessEvent.event_type == "ai_customer_note_added"
        )) == 1


@pytest.mark.asyncio
async def test_task_uses_ai_actor_and_survives_executor_restart(tool_factory):
    env = await seed_environment(tool_factory)
    body = {"arguments": {
        "customer_id": env["customer"].id, "lead_id": env["lead"].id,
        "thread_id": env["lead"].thread_id,
        "assigned_to_user_id": env["founder"].id,
        "task_type": "follow_up", "title": "Call customer tomorrow",
        "priority": "normal",
        "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }}
    request_headers = headers(env["principal"], "task-restart-001")
    app1 = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app1), base_url="http://test"
    ) as client:
        first = await client.post(
            "/ai/tools/create_task/execute", headers=request_headers, json=body
        )
    app2 = application(tool_factory, AuthorityService(tool_factory))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2), base_url="http://test"
    ) as client:
        replay = await client.post(
            "/ai/tools/create_task/execute", headers=request_headers, json=body
        )
    assert first.status_code == replay.status_code == 200
    assert first.json()["result"]["id"] == replay.json()["result"]["id"]
    async with tool_factory() as session:
        tasks = (await session.scalars(select(CRMTask))).all()
        assert len(tasks) == 1
        assert tasks[0].created_by_user_id is None
        assert tasks[0].created_by_principal_id == env["principal"]["id"]


@pytest.mark.asyncio
async def test_prepare_quotation_creates_draft_but_never_dispatches(tool_factory):
    env = await seed_environment(tool_factory)
    app = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ai/tools/prepare_quotation/execute",
            headers=headers(env["principal"], "prepare-quote-001"),
            json={"arguments": {
                "lead_id": env["lead"].id, "product_code": "MSB-001",
                "quantity": "10", "requested_discount_pct": "2",
                "validity_days": 15,
            }},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["status"] == "draft"
    assert result["dispatched"] is False
    async with tool_factory() as session:
        quotation = await session.get(QuotationRecord, result["quotation_id"])
        assert quotation.status == QuotationStatus.DRAFT
        assert quotation.sent_at is None
        assert quotation.sent_via is None
        assert quotation.prepared_by_principal_id == env["principal"]["id"]


@pytest.mark.asyncio
async def test_activity_and_followup_record_ai_actor_and_audit(tool_factory):
    env = await seed_environment(tool_factory)
    async with tool_factory() as session:
        quotation = QuotationRecord(
            business_id="tenant-a", customer_id=env["customer"].id,
            thread_id=env["lead"].thread_id, quotation_number="QT-SENT-TOOLS",
            inquiry_id=env["lead"].inquiry_id, status=QuotationStatus.SENT,
            buyer_company=env["customer"].company_name, total_inc_gst=100000,
            requires_approval=False, draft_json="{}", html_content="",
            sent_via="email", sent_to="buyer@visible.example",
            sent_at=datetime.now(UTC),
        )
        session.add(quotation)
        await session.commit()
        quotation_id = quotation.id
    app = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        activity = await client.post(
            "/ai/tools/record_activity/execute",
            headers=headers(env["principal"], "activity-001"),
            json={"arguments": {
                "customer_id": env["customer"].id,
                "lead_id": env["lead"].id,
                "thread_id": env["lead"].thread_id,
                "activity_type": "call", "subject": "Discussed delivery date",
                "outcome": "customer_interested",
            }},
        )
        followup = await client.post(
            "/ai/tools/schedule_followup/execute",
            headers=headers(env["principal"], "followup-001"),
            json={"arguments": {"quotation_id": quotation_id}},
        )
    assert activity.status_code == followup.status_code == 200
    assert followup.json()["result"]["jobs_created"] == 4
    async with tool_factory() as session:
        saved_activity = await session.scalar(select(CRMActivity))
        jobs = (await session.scalars(select(FollowUpJob))).all()
        assert saved_activity.actor_principal_id == env["principal"]["id"]
        assert len(jobs) == 4
        assert all(job.created_by_principal_id == env["principal"]["id"] for job in jobs)
        event_types = set((await session.scalars(select(BusinessEvent.event_type).where(
            BusinessEvent.event_type.in_(("ai_crm_activity_recorded", "ai_followup_scheduled"))
        ))).all())
        assert event_types == {"ai_crm_activity_recorded", "ai_followup_scheduled"}


@pytest.mark.asyncio
async def test_recommend_only_mode_denies_low_risk_mutation(tool_factory):
    env = await seed_environment(tool_factory, operating_mode="recommend_only")
    app = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/ai/tools/add_customer_note/execute",
            headers=headers(env["principal"], "denied-note-001"),
            json={"arguments": {
                "customer_id": env["customer"].id,
                "content": "This must not be stored.",
            }},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    async with tool_factory() as session:
        assert await session.scalar(select(func.count()).select_from(CustomerNote)) == 0
        execution = await session.scalar(select(AIToolExecution).where(
            AIToolExecution.tool_name == "add_customer_note"
        ))
        assert execution.status == "denied"


@pytest.mark.asyncio
async def test_batch4_tool_call_creates_one_durable_action_ledger(tool_factory):
    env = await seed_environment(tool_factory)
    app = application(tool_factory, env["authority"])
    request_headers = headers(env["principal"], "batch4-note-once")
    body = {"reason": "Remember a customer communication preference", "arguments": {
        "customer_id": env["customer"].id,
        "content": "Customer prefers concise email quotations.",
    }}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.post(
            "/ai/tools/add_customer_note/execute", headers=request_headers, json=body
        )
        replay = await client.post(
            "/ai/tools/add_customer_note/execute", headers=request_headers, json=body
        )
    assert first.status_code == replay.status_code == 200
    assert first.json()["action_request_id"] == replay.json()["action_request_id"]
    async with tool_factory() as session:
        actions = (await session.scalars(select(AIActionRequest))).all()
        executions = (await session.scalars(select(AIToolExecution))).all()
        assert len(actions) == len(executions) == 1
        assert actions[0].status == "SUCCEEDED"
        assert actions[0].reason == "Remember a customer communication preference"
        assert actions[0].latest_tool_execution_id == executions[0].id
        assert executions[0].action_request_id == actions[0].id


@pytest.mark.asyncio
async def test_batch4_approval_survives_restart_and_is_consumed_once(tool_factory):
    env = await seed_environment(tool_factory)
    body = {"reason": "Prepare a negotiated draft", "arguments": {
        "lead_id": env["lead"].id, "product_code": "MSB-001",
        "quantity": "10", "requested_discount_pct": "10",
        "validity_days": 15,
    }}
    app1 = application(tool_factory, env["authority"])
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app1), base_url="http://test"
    ) as client:
        proposed = await client.post(
            "/ai/tools/prepare_quotation/execute",
            headers=headers(env["principal"], "batch4-proposal-001"), json=body,
        )
    assert proposed.status_code == 200
    assert proposed.json()["status"] == "pending_approval"
    action_id = proposed.json()["action_request_id"]
    approval_id = proposed.json()["authority"]["approval_request_id"]

    owner = AuthenticatedUser(
        user_id=env["founder"].id, business_id="tenant-a", membership_id="restart",
        role="admin", email=env["founder"].email,
        display_name=env["founder"].display_name,
    )
    restarted_authority = AuthorityService(tool_factory)
    resolved = await restarted_authority.resolve_approval(
        owner, approval_id, approve=True,
        reason="Founder approved this exact low-margin draft",
    )
    assert resolved["status"] == "APPROVED"

    # A fresh executor simulates an application restart. The action and
    # approval live in PostgreSQL/SQLAlchemy, not in process memory.
    app2 = application(tool_factory, restarted_authority)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app2), base_url="http://test"
    ) as client:
        resumed = await client.post(
            f"/ai/tools/actions/{action_id}/execute",
            headers=headers(env["principal"], "batch4-resume-001"),
            json={"arguments": {}},
        )
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "completed"
    assert resumed.json()["authority"]["policy_code"] == "APPROVED_POLICY_EXCEPTION"
    async with tool_factory() as session:
        action = await session.get(AIActionRequest, action_id)
        approval = await session.get(AuthorityApprovalRequest, approval_id)
        decisions = (await session.scalars(select(ApprovalDecision))).all()
        assert action.status == "SUCCEEDED"
        assert action.execution_attempt_count == 1
        assert approval.status == "CONSUMED"
        assert len(decisions) == 1
        assert decisions[0].decision == "APPROVED"

    with pytest.raises(Exception) as second_resolution:
        await restarted_authority.resolve_approval(
            owner, approval_id, approve=True, reason="Must not approve twice",
        )
    assert getattr(second_resolution.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_batch4_changed_price_requires_fresh_approval(tool_factory):
    env = await seed_environment(tool_factory)
    app = application(tool_factory, env["authority"])
    body = {"arguments": {
        "lead_id": env["lead"].id, "product_code": "MSB-001",
        "quantity": "10", "requested_discount_pct": "10",
        "validity_days": 15,
    }}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        proposed = await client.post(
            "/ai/tools/prepare_quotation/execute",
            headers=headers(env["principal"], "batch4-change-001"), json=body,
        )
    action_id = proposed.json()["action_request_id"]
    first_approval_id = proposed.json()["authority"]["approval_request_id"]
    owner = AuthenticatedUser(
        user_id=env["founder"].id, business_id="tenant-a", membership_id="change",
        role="admin", email=env["founder"].email,
        display_name=env["founder"].display_name,
    )
    await env["authority"].resolve_approval(
        owner, first_approval_id, approve=True, reason="Approved original price snapshot",
    )
    async with tool_factory() as session:
        price = await session.scalar(select(ProductPriceRecord).where(
            ProductPriceRecord.business_id == "tenant-a",
            ProductPriceRecord.product_code == "MSB-001",
        ))
        price.base_price_inr = Decimal("75000")
        await session.commit()

    restarted = application(tool_factory, AuthorityService(tool_factory))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted), base_url="http://test"
    ) as client:
        revalidated = await client.post(
            f"/ai/tools/actions/{action_id}/execute",
            headers=headers(env["principal"], "batch4-change-resume"),
            json={"arguments": {}},
        )
    assert revalidated.status_code == 200
    assert revalidated.json()["status"] == "pending_approval"
    assert revalidated.json()["authority"]["approval_request_id"] != first_approval_id
    async with tool_factory() as session:
        action = await session.get(AIActionRequest, action_id)
        assert action.status == "AWAITING_APPROVAL"
        assert action.execution_result_json is None
        assert await session.scalar(select(func.count()).select_from(QuotationRecord)) == 0
