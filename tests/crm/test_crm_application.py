import os
from datetime import datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("CRM_PASSWORD_ITERATIONS", "1000")

from app.crm.assignment import auto_assign_lead
from app.crm.auth import AuthenticatedUser, create_auth_session, hash_password
from app.crm.router import router
from app.crm.service import CRMService
from app.database import Base
from app.database.models.crm import BusinessMembership, CRMTask, User
from app.database.models.customer import Customer
from app.database.models.customer import CustomerMatchReview, CustomerMatchReviewStatus
from app.database.models.activity import BusinessEvent
from app.customers.merge_service import resolve_customer_match_review
from app.database.models.lead import InquirySource, Lead, LeadStatus
from app.database.models.pipeline import PipelineInstance


@pytest_asyncio.fixture
async def crm_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'crm.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def seed_user(factory, *, business_id, email, role, name="CRM User"):
    async with factory() as session:
        user = User(
            email=email,
            normalized_email=email.lower(),
            display_name=name,
            password_hash=hash_password("valid-password-123"),
        )
        session.add(user)
        await session.flush()
        membership = BusinessMembership(
            business_id=business_id,
            user_id=user.id,
            role=role,
        )
        session.add(membership)
        await session.commit()
        return user, membership


def auth_user(user, membership):
    return AuthenticatedUser(
        user_id=user.id,
        business_id=membership.business_id,
        membership_id=membership.id,
        role=membership.role,
        email=user.email,
        display_name=user.display_name,
    )


@pytest.mark.asyncio
async def test_login_and_customer_api_derive_tenant_from_session(crm_factory):
    user, membership = await seed_user(
        crm_factory,
        business_id="tenant-a",
        email="manager@example.com",
        role="sales_manager",
    )
    async with crm_factory() as session:
        session.add_all([
            Customer(business_id="tenant-a", company_name="Visible Steel"),
            Customer(business_id="tenant-b", company_name="Hidden Steel"),
        ])
        await session.commit()

    application = FastAPI()
    application.state.session_factory = crm_factory
    application.state.crm_service = CRMService(crm_factory)
    application.include_router(router)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post("/crm/auth/login", json={
            "business_id": "tenant-a",
            "email": user.email,
            "password": "valid-password-123",
        })
        assert login.status_code == 200
        token = login.json()["access_token"]
        response = await client.get(
            "/crm/customers",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 200
    assert [item["company_name"] for item in response.json()["items"]] == ["Visible Steel"]


@pytest.mark.asyncio
async def test_salesperson_only_reads_assigned_customer(crm_factory):
    user, membership = await seed_user(
        crm_factory,
        business_id="tenant-a",
        email="sales@example.com",
        role="salesperson",
    )
    async with crm_factory() as session:
        visible = Customer(
            business_id="tenant-a", company_name="Assigned Customer",
            account_owner_id=user.id,
        )
        hidden = Customer(business_id="tenant-a", company_name="Other Customer")
        session.add_all([visible, hidden])
        await session.commit()
    result = await CRMService(crm_factory).list_customers(
        auth_user(user, membership), search=None, city=None, owner_id=None,
        page=1, page_size=25,
    )
    assert [item["company_name"] for item in result["items"]] == ["Assigned Customer"]


@pytest.mark.asyncio
async def test_deterministic_assignment_prefers_account_owner(crm_factory):
    salesperson, membership = await seed_user(
        crm_factory,
        business_id="tenant-a",
        email="owner@example.com",
        role="salesperson",
    )
    async with crm_factory() as session:
        customer = Customer(
            business_id="tenant-a",
            company_name="Existing Account",
            account_owner_id=salesperson.id,
        )
        session.add(customer)
        await session.flush()
        lead = Lead(
            business_id="tenant-a",
            customer_id=customer.id,
            thread_id="thread-assignment",
            inquiry_id="inquiry-assignment",
            source=InquirySource.WEBSITE,
            status=LeadStatus.NEW,
            raw_text="Need steel",
        )
        session.add(lead)
        await session.flush()
        assigned = await auto_assign_lead(
            session, business_id="tenant-a", lead_id=lead.id
        )
        await session.commit()
        assert assigned == salesperson.id
        assert lead.assigned_to_user_id == salesperson.id


@pytest.mark.asyncio
async def test_task_optimistic_lock_rejects_stale_update(crm_factory):
    manager, membership = await seed_user(
        crm_factory,
        business_id="tenant-a",
        email="task-manager@example.com",
        role="sales_manager",
    )
    actor = auth_user(manager, membership)
    service = CRMService(crm_factory)
    created = await service.create_task(actor, {
        "customer_id": None,
        "lead_id": None,
        "thread_id": None,
        "assigned_to_user_id": manager.id,
        "task_type": "follow_up",
        "title": "Call customer",
        "description": None,
        "priority": "normal",
        "due_at": datetime.utcnow() + timedelta(days=1),
    })
    updated = await service.update_task(
        actor, created["id"], {"priority": "high"}, expected_version=1
    )
    assert updated["version"] == 2
    with pytest.raises(HTTPException) as error:
        await service.update_task(
            actor, created["id"], {"priority": "urgent"}, expected_version=1
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_close_lost_updates_pipeline_and_creates_audit(crm_factory):
    manager, membership = await seed_user(
        crm_factory,
        business_id="tenant-a",
        email="closer@example.com",
        role="sales_manager",
    )
    async with crm_factory() as session:
        customer = Customer(business_id="tenant-a", company_name="Lost Account")
        session.add(customer)
        await session.flush()
        lead = Lead(
            business_id="tenant-a", customer_id=customer.id,
            thread_id="thread-lost", inquiry_id="inquiry-lost",
            source=InquirySource.EMAIL, status=LeadStatus.NEW, raw_text="Request",
        )
        session.add(lead)
        await session.flush()
        session.add(PipelineInstance(
            business_id="tenant-a", thread_id=lead.thread_id,
            customer_id=customer.id, lead_id=lead.id,
            pipeline_status="awaiting_customer_reply", waiting_for="customer",
        ))
        await session.commit()
        lead_id = lead.id
    result = await CRMService(crm_factory).close_lost(
        auth_user(manager, membership), lead_id,
        reason_code="price", notes="Competitor price was lower",
        competitor_name="Competitor Ltd", lost_value=None,
    )
    assert result["pipeline_status"] == "closed_lost"
    assert result["lost_reason_code"] == "price"
    async with crm_factory() as session:
        pipeline = await session.scalar(select(PipelineInstance).where(
            PipelineInstance.thread_id == "thread-lost"
        ))
        assert pipeline.pipeline_status == "closed_lost"


@pytest.mark.asyncio
async def test_controlled_customer_merge_moves_crm_records_and_audits(crm_factory):
    manager, membership = await seed_user(
        crm_factory,
        business_id="tenant-a",
        email="merge-manager@example.com",
        role="sales_manager",
    )
    async with crm_factory() as session:
        provisional = Customer(business_id="tenant-a", company_name="ABC Steel Pvt Ltd")
        target = Customer(business_id="tenant-a", company_name="ABC Steel")
        session.add_all([provisional, target])
        await session.flush()
        task = CRMTask(
            business_id="tenant-a",
            customer_id=provisional.id,
            assigned_to_user_id=manager.id,
            created_by_user_id=manager.id,
            task_type="follow_up",
            title="Verify duplicate",
            due_at=datetime.utcnow() + timedelta(days=1),
        )
        review = CustomerMatchReview(
            business_id="tenant-a",
            provisional_customer_id=provisional.id,
            candidate_customer_id=target.id,
            confidence=0.9,
            matched_signals=["company_name"],
            conflicting_signals=[],
            status=CustomerMatchReviewStatus.PENDING,
        )
        session.add_all([task, review])
        await session.commit()
        task_id, review_id, target_id = task.id, review.id, target.id

    async with crm_factory() as session:
        await resolve_customer_match_review(
            session,
            review_id=review_id,
            business_id="tenant-a",
            action="merge",
            resolved_by=manager.id,
            notes="Verified by sales manager",
        )
    async with crm_factory() as session:
        moved_task = await session.get(CRMTask, task_id)
        audit = await session.scalar(select(BusinessEvent).where(
            BusinessEvent.business_id == "tenant-a",
            BusinessEvent.event_type == "crm.customer_merged",
        ))
        assert moved_task.customer_id == target_id
        assert audit is not None
        assert audit.data["target_customer_id"] == target_id
