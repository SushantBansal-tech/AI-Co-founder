import os
import asyncio

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("CRM_PASSWORD_ITERATIONS", "1000")

from app.authority.auth import credential_digest
from app.authority.defaults import DEFAULT_POLICIES
from app.authority.router import execution_router, router
from app.authority.service import AuthorityService
from app.crm.auth import AuthenticatedUser, create_auth_session, hash_password
from app.database import Base
from app.database.models.activity import BusinessEvent
from app.database.models.authority import (
    AIServicePrincipal,
    AuthorityApprovalRequest,
    AuthorityDecision,
    AuthorityPolicyVersion,
    BusinessSettings,
)
from app.database.models.crm import BusinessMembership, User


@pytest_asyncio.fixture
async def authority_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'authority.db'}")
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def seed_admin(factory, business_id="tenant-a", role="admin"):
    async with factory() as session:
        user = User(
            email=f"{business_id}@example.com",
            normalized_email=f"{business_id}@example.com",
            display_name="Founder",
            password_hash=hash_password("valid-password-123"),
        )
        session.add(user)
        await session.flush()
        membership = BusinessMembership(
            business_id=business_id, user_id=user.id, role=role
        )
        session.add(membership)
        await session.commit()
    return user, membership


def actor(user, membership):
    return AuthenticatedUser(
        user_id=user.id,
        business_id=membership.business_id,
        membership_id=membership.id,
        role=membership.role,
        email=user.email,
        display_name=user.display_name,
    )


def executable_settings(version=1):
    return {
        "ai_operating_mode": "execute_low_risk",
        "currency": "INR",
        "timezone": "Asia/Kolkata",
        "maximum_automatic_discount_pct": 3,
        "maximum_automatic_quotation_value": 5_000_000,
        "minimum_margin_pct": 12,
        "daily_outbound_message_limit": 100,
        "default_approval_role": "admin",
        "expected_version": version,
        "change_reason": "Founder enabled tested low-risk execution",
    }


@pytest.mark.asyncio
async def test_defaults_are_safe_versioned_audited_and_tenant_isolated(authority_factory):
    user_a, membership_a = await seed_admin(authority_factory, "tenant-a")
    user_b, membership_b = await seed_admin(authority_factory, "tenant-b")
    service = AuthorityService(authority_factory)
    settings_a = await service.get_settings(actor(user_a, membership_a))
    settings_b = await service.get_settings(actor(user_b, membership_b))
    assert settings_a["ai_operating_mode"] == "recommend_only"
    assert settings_a["maximum_automatic_discount_pct"] == 3.0
    assert settings_b["business_id"] == "tenant-b"
    assert len(await service.list_policies(actor(user_a, membership_a))) == len(DEFAULT_POLICIES)
    async with authority_factory() as session:
        assert len((await session.scalars(select(BusinessSettings))).all()) == 2
        events = (await session.scalars(select(BusinessEvent).where(
            BusinessEvent.event_type == "ai_authority_defaults_initialized"
        ))).all()
        assert {event.business_id for event in events} == {"tenant-a", "tenant-b"}


@pytest.mark.asyncio
async def test_settings_use_optimistic_lock_and_immutable_history(authority_factory):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    await service.get_settings(owner)
    updated = await service.update_settings(owner, executable_settings())
    assert updated["version"] == 2
    assert updated["ai_operating_mode"] == "execute_low_risk"
    with pytest.raises(HTTPException) as stale:
        await service.update_settings(owner, executable_settings())
    assert stale.value.status_code == 409
    history = await service.settings_history(owner)
    assert [row["version"] for row in history] == [2, 1]
    assert history[1]["settings"]["ai_operating_mode"] == "recommend_only"


@pytest.mark.asyncio
async def test_jarvis_has_separate_one_time_credential_and_forbidden_scopes(authority_factory):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    created = await service.create_principal(
        owner, name="Jarvis Sales", scopes=["quotation:prepare", "reminder:send"]
    )
    assert created["credential"].startswith("jarvis_live_")
    listed = await service.list_principals(owner)
    assert "credential" not in listed[0]
    async with authority_factory() as session:
        stored = await session.get(AIServicePrincipal, created["id"])
        assert stored.credential_hash == credential_digest(created["credential"])
        assert stored.credential_hash != created["credential"]
    with pytest.raises(HTTPException) as forbidden:
        await service.change_scope(
            owner, created["id"], "authority:modify", "Do not allow this", grant=True
        )
    assert forbidden.value.status_code == 422


@pytest.mark.asyncio
async def test_deterministic_policy_enforces_mode_scope_margin_and_value(authority_factory):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    await service.get_settings(owner)
    principal = await service.create_principal(
        owner, name="Jarvis", scopes=["quotation:prepare"]
    )
    recommendation = await service.evaluate(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="quotation_create",
        facts={"quotation_value": 1_000_000, "resulting_margin_pct": 15},
    )
    assert recommendation["decision"] == "recommend_only"
    await service.update_settings(owner, executable_settings())
    allowed = await service.evaluate(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="quotation_create",
        facts={"quotation_value": 1_000_000, "resulting_margin_pct": 15},
    )
    assert allowed["decision"] == "allow"
    too_large = await service.evaluate(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="quotation_create",
        facts={"quotation_value": 6_000_000, "resulting_margin_pct": 15},
    )
    assert too_large["decision"] == "approval_required"
    low_margin = await service.evaluate(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="quotation_create",
        facts={"quotation_value": 1_000_000, "resulting_margin_pct": 10},
    )
    assert low_margin["decision"] == "approval_required"


@pytest.mark.asyncio
async def test_policy_updates_append_versions_instead_of_overwriting(authority_factory):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    policies = await service.list_policies(owner)
    negotiation = next(p for p in policies if p["action_type"] == "negotiation_response")
    changed = await service.update_policy(owner, negotiation["id"], {
        "decision_mode": "approval_required",
        "risk_level": "high",
        "required_scope": "negotiation:prepare",
        "approval_role": "sales_manager",
        "conditions": {"manager_review": True},
        "expected_version": 1,
        "change_reason": "Require manager review for every negotiation",
    })
    assert changed["active_version"] == 2
    history = await service.policy_history(owner, negotiation["id"])
    assert [row["version"] for row in history] == [2, 1]
    async with authority_factory() as session:
        assert len((await session.scalars(select(AuthorityPolicyVersion).where(
            AuthorityPolicyVersion.policy_id == negotiation["id"]
        ))).all()) == 2


@pytest.mark.asyncio
async def test_ai_auth_survives_service_restart_and_revocation(authority_factory):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    await service.get_settings(owner)
    created = await service.create_principal(
        owner, name="Jarvis", scopes=["quotation:prepare"]
    )
    application = FastAPI()
    application.state.session_factory = authority_factory
    application.state.authority_service = AuthorityService(authority_factory)
    application.include_router(execution_router)
    transport = httpx.ASGITransport(app=application)
    headers = {"X-AI-Principal-Token": created["credential"]}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/ai/authority/evaluate", headers=headers,
            json={"action_type": "quotation_create", "facts": {
                "quotation_value": 1000, "resulting_margin_pct": 15,
            }},
        )
    assert response.status_code == 200
    assert response.json()["decision"] == "recommend_only"
    await service.revoke_principal(owner, created["id"], "Credential retired")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        denied = await client.post(
            "/ai/authority/evaluate", headers=headers,
            json={"action_type": "quotation_create", "facts": {}},
        )
    assert denied.status_code == 401


@pytest.mark.asyncio
async def test_non_admin_cannot_open_founder_authority_api(authority_factory):
    user, membership = await seed_admin(authority_factory, role="sales_manager")
    async with authority_factory() as session:
        token, _ = await create_auth_session(
            session, user_id=user.id, business_id=membership.business_id
        )
        await session.commit()
    application = FastAPI()
    application.state.session_factory = authority_factory
    application.state.authority_service = AuthorityService(authority_factory)
    application.include_router(router)
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/crm/ai/settings", headers={"Authorization": f"Bearer {token}"}
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_principal_creation_api_is_idempotent_under_duplicate_requests(authority_factory):
    user, membership = await seed_admin(authority_factory)
    async with authority_factory() as session:
        token, _ = await create_auth_session(
            session, user_id=user.id, business_id=membership.business_id
        )
        await session.commit()
    application = FastAPI()
    application.state.session_factory = authority_factory
    application.state.authority_service = AuthorityService(authority_factory)
    application.include_router(router)
    transport = httpx.ASGITransport(app=application)
    headers = {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": "create-jarvis-once",
    }
    body = {"name": "Jarvis", "scopes": ["quotation:prepare"]}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first, duplicate = await asyncio.gather(
            client.post("/crm/ai/principals", headers=headers, json=body),
            client.post("/crm/ai/principals", headers=headers, json=body),
        )
        if 409 in {first.status_code, duplicate.status_code}:
            # A concurrent request may observe the short-lived processing claim.
            retry = await client.post("/crm/ai/principals", headers=headers, json=body)
            assert retry.status_code == 200
        else:
            assert first.status_code == duplicate.status_code == 200
            assert first.json()["id"] == duplicate.json()["id"]
    async with authority_factory() as session:
        principals = (await session.scalars(select(AIServicePrincipal))).all()
        assert len(principals) == 1


@pytest.mark.asyncio
async def test_batch3_records_standard_decision_and_blocks_missing_master_data(
    authority_factory,
):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    await service.get_settings(owner)
    await service.update_settings(owner, executable_settings())
    principal = await service.create_principal(
        owner, name="Jarvis Batch 3", scopes=["quotation:send"]
    )

    result = await service.evaluate_action(
        business_id="tenant-a",
        principal_id=principal["id"],
        action_type="quotation_send",
        facts={
            "entity_type": "quotation",
            "entity_id": "quote-1",
            "thread_id": "thread-1",
            "quotation_value": 100000,
            "resulting_margin_pct": 15,
            "missing_master_data": ["gst_rate"],
        },
    )

    assert result.decision.value == "BLOCKED_MASTER_DATA"
    assert result.policy_code == "REQUIRED_MASTER_DATA_MISSING"
    assert result.decision_id
    assert result.approval_request_id is None
    async with authority_factory() as session:
        row = await session.get(AuthorityDecision, result.decision_id)
        assert row.business_id == "tenant-a"
        assert row.policy_version == 1
        assert row.missing_master_data == ["gst_rate"]


@pytest.mark.asyncio
async def test_batch3_approval_is_bound_to_exact_facts_and_consumed_once(
    authority_factory,
):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    await service.get_settings(owner)
    await service.update_settings(owner, executable_settings())
    principal = await service.create_principal(
        owner, name="Jarvis Discount", scopes=["discount:apply"]
    )
    facts = {
        "entity_type": "quotation",
        "entity_id": "quote-2",
        "quotation_value": 100000,
        "discount_pct": 6,
        "resulting_margin_pct": 15,
    }

    requested = await service.evaluate_action(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="discount_apply", facts=facts,
    )
    assert requested.decision.value == "REQUIRE_APPROVAL"
    assert requested.policy_code == "DISCOUNT_ABOVE_AI_AUTHORITY"
    assert requested.approval_request_id

    approved = await service.resolve_approval(
        owner, requested.approval_request_id,
        approve=True, reason="Founder approved this exact negotiated quotation",
    )
    assert approved["status"] == "APPROVED"

    consumed = await service.evaluate_action(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="discount_apply", facts=facts,
    )
    assert consumed.decision.value == "ALLOW"
    assert consumed.policy_code == "APPROVED_POLICY_EXCEPTION"

    changed = await service.evaluate_action(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="discount_apply", facts={**facts, "discount_pct": 7},
    )
    assert changed.decision.value == "REQUIRE_APPROVAL"
    assert changed.approval_request_id != requested.approval_request_id
    async with authority_factory() as session:
        original = await session.get(
            AuthorityApprovalRequest, requested.approval_request_id
        )
        assert original.status == "CONSUMED"


@pytest.mark.asyncio
async def test_batch3_tenant_cannot_read_or_resolve_another_tenant_decision(
    authority_factory,
):
    user_a, membership_a = await seed_admin(authority_factory, "tenant-a")
    user_b, membership_b = await seed_admin(authority_factory, "tenant-b")
    owner_a = actor(user_a, membership_a)
    owner_b = actor(user_b, membership_b)
    service = AuthorityService(authority_factory)
    await service.get_settings(owner_a)
    await service.update_settings(owner_a, executable_settings())
    principal = await service.create_principal(
        owner_a, name="Jarvis A", scopes=["discount:apply"]
    )
    result = await service.evaluate_action(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="discount_apply",
        facts={"discount_pct": 6, "resulting_margin_pct": 15},
    )

    with pytest.raises(HTTPException) as hidden:
        await service.get_decision(owner_b, result.decision_id)
    assert hidden.value.status_code == 404
    with pytest.raises(HTTPException) as hidden_approval:
        await service.resolve_approval(
            owner_b, result.approval_request_id,
            approve=True, reason="Must not cross tenants",
        )
    assert hidden_approval.value.status_code == 404


@pytest.mark.asyncio
async def test_batch3_hard_safety_rule_cannot_be_overridden_by_approval_policy(
    authority_factory,
):
    user, membership = await seed_admin(authority_factory)
    owner = actor(user, membership)
    service = AuthorityService(authority_factory)
    await service.get_settings(owner)
    await service.update_settings(owner, executable_settings())
    principal = await service.create_principal(
        owner, name="Jarvis Merge", scopes=["customer:read"]
    )
    result = await service.evaluate_action(
        business_id="tenant-a", principal_id=principal["id"],
        action_type="customer_merge",
        facts={
            "source_customer_id": "customer-a",
            "target_customer_id": "customer-b",
            "conflicting_identities": ["GSTIN values conflict"],
        },
    )
    assert result.decision.value == "DENY"
    assert result.policy_code == "CUSTOMER_IDENTITIES_CONFLICT"
    assert result.approval_request_id is None
