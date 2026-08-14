from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, exists, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.crm.auth import (
    AuthenticatedUser,
    ROLES,
    hash_password,
    normalize_user_email,
    verify_password,
)
from app.database.models.activity import BusinessEvent, Interaction
from app.database.models.crm import (
    BusinessMembership,
    CRMActivity,
    CRMTask,
    LeadAssignment,
    User,
)
from app.database.models.customer import Customer, CustomerIdentity, PaymentRecord
from app.database.models.followup import FollowUpRecord
from app.database.models.followup_job import FollowUpJob
from app.database.models.lead import InquirySource, Lead, LeadStatus
from app.database.models.order import PurchaseOrder, SalesOrder
from app.database.models.pipeline import PipelineInstance
from app.database.models.quotation import QuotationRecord
from app.events.service import record_business_event
from app.followups.service import cancel_open_followup_jobs
from app.customers.normalization import (
    normalize_company_name,
    normalize_email,
    normalize_gstin,
    normalize_phone,
)


OPEN_TASK_STATUSES = {"open", "in_progress"}


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def iso(value: Any) -> str | None:
    return value.isoformat() if value else None


class CRMService:
    def __init__(self, session_factory: async_sessionmaker):
        self.session_factory = session_factory

    @staticmethod
    def _can_read_all(user: AuthenticatedUser) -> bool:
        return user.has_permission("crm:read_all")

    @staticmethod
    def customer_payload(customer: Customer, owner_name: str | None = None) -> dict:
        return {
            "id": customer.id,
            "company_name": customer.company_name,
            "contact_person": customer.contact_person,
            "email": customer.email,
            "phone": customer.phone,
            "gstin": customer.gstin,
            "city": customer.city,
            "state": customer.state,
            "customer_type": customer.customer_type,
            "status": customer.status,
            "account_owner_id": customer.account_owner_id,
            "account_owner_name": owner_name,
            "credit_limit": float(customer.credit_limit or 0),
            "outstanding_amount": float(customer.outstanding_amount or 0),
            "payment_behavior": enum_value(customer.payment_behavior),
            "created_at": iso(customer.created_at),
            "updated_at": iso(customer.updated_at),
        }

    @staticmethod
    def lead_payload(
        lead: Lead,
        customer: Customer | None = None,
        pipeline: PipelineInstance | None = None,
        owner_name: str | None = None,
    ) -> dict:
        return {
            "id": lead.id,
            "customer_id": lead.customer_id,
            "customer_company": customer.company_name if customer else lead.company_name,
            "thread_id": lead.thread_id,
            "inquiry_id": lead.inquiry_id,
            "source": enum_value(lead.source),
            "contact_person": lead.contact_person,
            "sender_identifier": lead.sender_identifier,
            "product_requested": lead.product_requested,
            "quantity": lead.quantity,
            "specifications": lead.specifications,
            "delivery_location": lead.delivery_location,
            "delivery_date": lead.delivery_date,
            "payment_expectation": lead.payment_expectation,
            "status": enum_value(lead.status),
            "assigned_to_user_id": lead.assigned_to_user_id,
            "assigned_to_name": owner_name,
            "assigned_at": iso(lead.assigned_at),
            "pipeline_status": pipeline.pipeline_status if pipeline else None,
            "business_milestone": pipeline.business_milestone if pipeline else None,
            "waiting_for": pipeline.waiting_for if pipeline else None,
            "status_reason": pipeline.status_reason if pipeline else None,
            "current_node": pipeline.current_node if pipeline else None,
            "approval_stage": pipeline.approval_stage if pipeline else None,
            "lost_reason_code": lead.lost_reason_code,
            "lost_reason_notes": lead.lost_reason_notes,
            "competitor_name": lead.competitor_name,
            "lost_value": float(lead.lost_value) if lead.lost_value is not None else None,
            "closed_lost_at": iso(lead.closed_lost_at),
            "created_at": iso(lead.created_at),
            "updated_at": iso(lead.updated_at),
        }

    async def list_members(self, user: AuthenticatedUser) -> list[dict]:
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(User, BusinessMembership)
                    .join(BusinessMembership, BusinessMembership.user_id == User.id)
                    .where(
                        BusinessMembership.business_id == user.business_id,
                        BusinessMembership.status == "active",
                        User.status == "active",
                    )
                    .order_by(User.display_name)
                )
            ).all()
        return [{
            "id": member.id,
            "email": member.email,
            "display_name": member.display_name,
            "role": membership.role,
            "membership_id": membership.id,
        } for member, membership in rows]

    async def create_member(
        self,
        actor: AuthenticatedUser,
        *,
        email: str,
        display_name: str,
        password: str,
        role: str,
    ) -> dict:
        if role not in ROLES:
            raise HTTPException(status_code=422, detail="Unsupported CRM role.")
        normalized = normalize_user_email(email)
        async with self.session_factory() as session:
            existing = await session.scalar(
                select(User).where(User.normalized_email == normalized)
            )
            if existing is None:
                existing = User(
                    email=email.strip(), normalized_email=normalized,
                    display_name=display_name.strip(), password_hash=hash_password(password),
                )
                session.add(existing)
                await session.flush()
            elif not verify_password(password, existing.password_hash):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "This email already belongs to a user. The existing "
                        "user must be invited through an account-verification flow."
                    ),
                )
            membership = await session.scalar(
                select(BusinessMembership).where(
                    BusinessMembership.business_id == actor.business_id,
                    BusinessMembership.user_id == existing.id,
                )
            )
            if membership is not None:
                raise HTTPException(status_code=409, detail="User already belongs to this business.")
            membership = BusinessMembership(
                business_id=actor.business_id, user_id=existing.id, role=role
            )
            session.add(membership)
            await record_business_event(
                session,
                business_id=actor.business_id,
                event_type="crm.user_added",
                source="crm",
                actor_type="employee",
                actor_id=actor.user_id,
                entity_type="user",
                entity_id=existing.id,
                data={"role": role},
            )
            await session.commit()
            return {
                "id": existing.id, "email": existing.email,
                "display_name": existing.display_name, "role": membership.role,
            }

    def _customer_scope(self, user: AuthenticatedUser):
        if self._can_read_all(user):
            return True
        return or_(
            Customer.account_owner_id == user.user_id,
            exists(select(Lead.id).where(
                Lead.business_id == user.business_id,
                Lead.customer_id == Customer.id,
                Lead.assigned_to_user_id == user.user_id,
            )),
        )

    def _lead_scope(self, user: AuthenticatedUser):
        if self._can_read_all(user):
            return True
        return Lead.assigned_to_user_id == user.user_id

    async def list_customers(
        self, user: AuthenticatedUser, *, search: str | None, city: str | None,
        owner_id: str | None, page: int, page_size: int,
    ) -> dict:
        owner = aliased(User)
        conditions = [
            Customer.business_id == user.business_id,
            Customer.status != "merged",
            self._customer_scope(user),
        ]
        if city:
            conditions.append(func.lower(Customer.city) == city.strip().lower())
        if owner_id:
            conditions.append(Customer.account_owner_id == owner_id)
        if search:
            pattern = f"%{search.strip().lower()}%"
            conditions.append(or_(
                func.lower(Customer.company_name).like(pattern),
                func.lower(func.coalesce(Customer.contact_person, "")).like(pattern),
                func.lower(func.coalesce(Customer.email, "")).like(pattern),
                func.lower(func.coalesce(Customer.phone, "")).like(pattern),
                func.lower(func.coalesce(Customer.gstin, "")).like(pattern),
                func.lower(Customer.id).like(pattern),
                exists(select(CustomerIdentity.id).where(
                    CustomerIdentity.business_id == user.business_id,
                    CustomerIdentity.customer_id == Customer.id,
                    func.lower(CustomerIdentity.normalized_value).like(pattern),
                )),
            ))
        async with self.session_factory() as session:
            total = await session.scalar(
                select(func.count()).select_from(Customer).where(*conditions)
            ) or 0
            rows = (
                await session.execute(
                    select(Customer, owner.display_name)
                    .outerjoin(owner, owner.id == Customer.account_owner_id)
                    .where(*conditions)
                    .order_by(Customer.updated_at.desc())
                    .offset((page - 1) * page_size)
                    .limit(page_size)
                )
            ).all()
        return {
            "items": [self.customer_payload(customer, owner_name) for customer, owner_name in rows],
            "page": page, "page_size": page_size, "total": total,
        }

    async def get_customer(self, user: AuthenticatedUser, customer_id: str) -> Customer:
        async with self.session_factory() as session:
            customer = await session.scalar(select(Customer).where(
                Customer.id == customer_id,
                Customer.business_id == user.business_id,
                self._customer_scope(user),
            ))
            if customer is None:
                raise HTTPException(status_code=404, detail="Customer not found.")
            session.expunge(customer)
            return customer

    async def update_customer(
        self, user: AuthenticatedUser, customer_id: str, changes: dict,
    ) -> dict:
        async with self.session_factory() as session:
            customer = await session.scalar(
                select(Customer).where(
                    Customer.id == customer_id,
                    Customer.business_id == user.business_id,
                ).with_for_update()
            )
            if customer is None:
                raise HTTPException(status_code=404, detail="Customer not found.")
            can_edit_all = user.has_permission("customer:edit")
            can_edit_assigned = (
                user.has_permission("customer:edit_assigned")
                and customer.account_owner_id == user.user_id
            )
            if not (can_edit_all or can_edit_assigned):
                raise HTTPException(status_code=403, detail="Customer edit permission denied.")
            if not can_edit_all:
                forbidden = set(changes) - {"contact_person", "email", "phone", "city", "state"}
                if forbidden:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Only contact fields may be edited: {sorted(forbidden)}",
                    )
            before = {name: getattr(customer, name) for name in changes}
            for name, value in changes.items():
                setattr(customer, name, value.strip() if isinstance(value, str) else value)
            identity_fields = {
                "email": ("email", normalize_email),
                "phone": ("phone", normalize_phone),
                "gstin": ("gstin", normalize_gstin),
                "company_name": ("company", normalize_company_name),
            }
            for field_name, (identity_type, normalizer) in identity_fields.items():
                if field_name in changes:
                    await self._sync_primary_identity(
                        session,
                        customer=customer,
                        identity_type=identity_type,
                        raw_value=getattr(customer, field_name),
                        normalizer=normalizer,
                    )
            await record_business_event(
                session,
                business_id=user.business_id,
                customer_id=customer.id,
                event_type="crm.customer_updated",
                source="crm",
                actor_type="employee",
                actor_id=user.user_id,
                entity_type="customer",
                entity_id=customer.id,
                data={"changed_fields": sorted(changes), "previous": before},
            )
            await session.commit()
            return self.customer_payload(customer)

    async def _sync_primary_identity(
        self,
        session: AsyncSession,
        *,
        customer: Customer,
        identity_type: str,
        raw_value: str | None,
        normalizer,
    ) -> None:
        normalized = normalizer(raw_value)
        if raw_value and not normalized:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {identity_type} value.",
            )
        current = await session.scalar(select(CustomerIdentity).where(
            CustomerIdentity.business_id == customer.business_id,
            CustomerIdentity.customer_id == customer.id,
            CustomerIdentity.identity_type == identity_type,
            CustomerIdentity.is_primary.is_(True),
        ).with_for_update())
        if normalized is None:
            if current:
                await session.delete(current)
            return
        conflict = await session.scalar(select(CustomerIdentity).where(
            CustomerIdentity.business_id == customer.business_id,
            CustomerIdentity.identity_type == identity_type,
            CustomerIdentity.normalized_value == normalized,
            CustomerIdentity.customer_id != customer.id,
        ))
        if conflict:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"The normalized {identity_type} already belongs to another "
                    "customer. Use the controlled duplicate-review workflow."
                ),
            )
        if current:
            current.raw_value = raw_value
            current.normalized_value = normalized
            current.source = "crm_manual_edit"
        else:
            session.add(CustomerIdentity(
                business_id=customer.business_id,
                customer_id=customer.id,
                identity_type=identity_type,
                raw_value=raw_value,
                normalized_value=normalized,
                is_verified=False,
                is_primary=True,
                source="crm_manual_edit",
            ))

    async def assign_customer(
        self, user: AuthenticatedUser, customer_id: str, owner_id: str, reason: str | None,
    ) -> dict:
        async with self.session_factory() as session:
            customer = await session.scalar(select(Customer).where(
                Customer.id == customer_id,
                Customer.business_id == user.business_id,
            ).with_for_update())
            if customer is None:
                raise HTTPException(status_code=404, detail="Customer not found.")
            await self._require_member(session, user.business_id, owner_id)
            previous = customer.account_owner_id
            customer.account_owner_id = owner_id
            await record_business_event(
                session, business_id=user.business_id, customer_id=customer.id,
                event_type="crm.customer_assigned", source="crm",
                actor_type="employee", actor_id=user.user_id,
                entity_type="customer", entity_id=customer.id,
                data={"previous_owner_id": previous, "owner_id": owner_id, "reason": reason},
            )
            await session.commit()
            return self.customer_payload(customer)

    async def list_leads(
        self, user: AuthenticatedUser, *, status: str | None, source: str | None,
        assigned_to: str | None, pipeline_status: str | None, unassigned: bool,
        overdue: bool, search: str | None, page: int, page_size: int,
    ) -> dict:
        owner = aliased(User)
        conditions = [Lead.business_id == user.business_id, self._lead_scope(user)]
        if status:
            try:
                conditions.append(Lead.status == LeadStatus(status))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Unsupported lead status.") from exc
        if source:
            try:
                conditions.append(Lead.source == InquirySource(source))
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="Unsupported inquiry source.") from exc
        if assigned_to:
            target = user.user_id if assigned_to == "me" else assigned_to
            conditions.append(Lead.assigned_to_user_id == target)
        if unassigned:
            conditions.append(Lead.assigned_to_user_id.is_(None))
        if pipeline_status:
            conditions.append(PipelineInstance.pipeline_status == pipeline_status)
        if overdue:
            conditions.append(exists(select(CRMTask.id).where(
                CRMTask.business_id == user.business_id,
                CRMTask.lead_id == Lead.id,
                CRMTask.status.in_(OPEN_TASK_STATUSES),
                CRMTask.due_at < utc_now(),
            )))
        if search:
            pattern = f"%{search.strip().lower()}%"
            conditions.append(or_(
                func.lower(func.coalesce(Lead.company_name, "")).like(pattern),
                func.lower(func.coalesce(Lead.contact_person, "")).like(pattern),
                func.lower(func.coalesce(Lead.product_requested, "")).like(pattern),
                func.lower(Lead.inquiry_id).like(pattern),
            ))
        async with self.session_factory() as session:
            base = (
                select(Lead, Customer, PipelineInstance, owner.display_name)
                .outerjoin(Customer, and_(
                    Customer.id == Lead.customer_id,
                    Customer.business_id == Lead.business_id,
                ))
                .outerjoin(PipelineInstance, and_(
                    PipelineInstance.thread_id == Lead.thread_id,
                    PipelineInstance.business_id == Lead.business_id,
                ))
                .outerjoin(owner, owner.id == Lead.assigned_to_user_id)
                .where(*conditions)
            )
            total = await session.scalar(
                select(func.count()).select_from(base.subquery())
            ) or 0
            rows = (
                await session.execute(
                    base.order_by(Lead.updated_at.desc())
                    .offset((page - 1) * page_size).limit(page_size)
                )
            ).all()
        return {
            "items": [self.lead_payload(*row) for row in rows],
            "page": page, "page_size": page_size, "total": total,
        }

    async def get_lead_payload(self, user: AuthenticatedUser, lead_id: str) -> dict:
        owner = aliased(User)
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Lead, Customer, PipelineInstance, owner.display_name)
                    .outerjoin(Customer, Customer.id == Lead.customer_id)
                    .outerjoin(PipelineInstance, and_(
                        PipelineInstance.business_id == Lead.business_id,
                        PipelineInstance.thread_id == Lead.thread_id,
                    ))
                    .outerjoin(owner, owner.id == Lead.assigned_to_user_id)
                    .where(
                        Lead.id == lead_id,
                        Lead.business_id == user.business_id,
                        self._lead_scope(user),
                    )
                )
            ).one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="Lead not found.")
        return self.lead_payload(*row)

    async def update_lead(self, user: AuthenticatedUser, lead_id: str, changes: dict) -> dict:
        async with self.session_factory() as session:
            lead = await session.scalar(select(Lead).where(
                Lead.id == lead_id, Lead.business_id == user.business_id,
            ).with_for_update())
            if lead is None:
                raise HTTPException(status_code=404, detail="Lead not found.")
            if not user.has_permission("lead:edit") and lead.assigned_to_user_id != user.user_id:
                raise HTTPException(status_code=403, detail="Lead edit permission denied.")
            for name, value in changes.items():
                setattr(lead, name, value)
            await record_business_event(
                session, business_id=user.business_id, customer_id=lead.customer_id,
                lead_id=lead.id, thread_id=lead.thread_id,
                event_type="crm.lead_updated", source="crm", actor_type="employee",
                actor_id=user.user_id, entity_type="lead", entity_id=lead.id,
                data={"changed_fields": sorted(changes)},
            )
            await session.commit()
        return await self.get_lead_payload(user, lead_id)

    async def assign_lead(
        self, user: AuthenticatedUser, lead_id: str, assignee_id: str, reason: str | None,
    ) -> dict:
        now = utc_now()
        async with self.session_factory() as session:
            lead = await session.scalar(select(Lead).where(
                Lead.id == lead_id, Lead.business_id == user.business_id,
            ).with_for_update())
            if lead is None:
                raise HTTPException(status_code=404, detail="Lead not found.")
            await self._require_member(session, user.business_id, assignee_id)
            await session.execute(update(LeadAssignment).where(
                LeadAssignment.business_id == user.business_id,
                LeadAssignment.lead_id == lead.id,
                LeadAssignment.ended_at.is_(None),
            ).values(ended_at=now))
            previous = lead.assigned_to_user_id
            lead.assigned_to_user_id = assignee_id
            lead.assigned_by_user_id = user.user_id
            lead.assigned_at = now
            session.add(LeadAssignment(
                business_id=user.business_id, lead_id=lead.id,
                assigned_to_user_id=assignee_id, assigned_by_user_id=user.user_id,
                reason=reason, started_at=now,
            ))
            await record_business_event(
                session, business_id=user.business_id, customer_id=lead.customer_id,
                lead_id=lead.id, thread_id=lead.thread_id,
                event_type="crm.lead_reassigned", source="crm", actor_type="employee",
                actor_id=user.user_id, entity_type="lead", entity_id=lead.id,
                data={"previous_assignee_id": previous, "assignee_id": assignee_id, "reason": reason},
            )
            session.add(CRMActivity(
                business_id=user.business_id, customer_id=lead.customer_id,
                lead_id=lead.id, thread_id=lead.thread_id,
                activity_type="lead_reassigned", subject="Lead reassigned",
                notes=reason, actor_user_id=user.user_id, occurred_at=now,
            ))
            await session.commit()
        return await self.get_lead_payload(user, lead_id)

    async def close_lost(
        self, user: AuthenticatedUser, lead_id: str, *, reason_code: str,
        notes: str | None, competitor_name: str | None, lost_value: Decimal | None,
    ) -> dict:
        now = utc_now()
        async with self.session_factory() as session:
            lead = await session.scalar(select(Lead).where(
                Lead.id == lead_id, Lead.business_id == user.business_id,
            ).with_for_update())
            if lead is None:
                raise HTTPException(status_code=404, detail="Lead not found.")
            if lead.closed_lost_at is not None:
                raise HTTPException(status_code=409, detail="Lead is already closed lost.")
            if enum_value(lead.status) == "won":
                raise HTTPException(status_code=409, detail="A won lead cannot be closed lost.")
            lead.lost_reason_code = reason_code
            lead.lost_reason_notes = notes
            lead.competitor_name = competitor_name
            lead.lost_value = lost_value
            lead.closed_lost_at = now
            lead.closed_lost_by_user_id = user.user_id
            pipeline = await session.scalar(select(PipelineInstance).where(
                PipelineInstance.business_id == user.business_id,
                PipelineInstance.thread_id == lead.thread_id,
            ).with_for_update())
            if pipeline:
                pipeline.pipeline_status = "closed_lost"
                pipeline.waiting_for = "none"
                pipeline.status_reason = reason_code
                pipeline.current_node = "crm_close_lost"
                pipeline.updated_at = now
                pipeline.version = (pipeline.version or 0) + 1
            cancelled = await cancel_open_followup_jobs(
                session, business_id=user.business_id, thread_id=lead.thread_id,
                reason=f"Lead closed lost: {reason_code}",
            )
            await record_business_event(
                session, business_id=user.business_id, customer_id=lead.customer_id,
                lead_id=lead.id, thread_id=lead.thread_id,
                event_type="crm.lead_closed_lost", source="crm", actor_type="employee",
                actor_id=user.user_id, entity_type="lead", entity_id=lead.id,
                data={
                    "reason_code": reason_code, "notes": notes,
                    "competitor_name": competitor_name,
                    "lost_value": float(lost_value) if lost_value is not None else None,
                    "cancelled_followups": cancelled,
                },
            )
            await session.commit()
        return await self.get_lead_payload(user, lead_id)

    async def reopen_lead(self, user: AuthenticatedUser, lead_id: str) -> dict:
        async with self.session_factory() as session:
            lead = await session.scalar(select(Lead).where(
                Lead.id == lead_id, Lead.business_id == user.business_id,
            ).with_for_update())
            if lead is None:
                raise HTTPException(status_code=404, detail="Lead not found.")
            if lead.closed_lost_at is None:
                raise HTTPException(status_code=409, detail="Lead is not closed lost.")
            previous_reason = lead.lost_reason_code
            lead.lost_reason_code = None
            lead.lost_reason_notes = None
            lead.competitor_name = None
            lead.lost_value = None
            lead.closed_lost_at = None
            lead.closed_lost_by_user_id = None
            pipeline = await session.scalar(select(PipelineInstance).where(
                PipelineInstance.business_id == user.business_id,
                PipelineInstance.thread_id == lead.thread_id,
            ).with_for_update())
            if pipeline:
                pipeline.pipeline_status = "processing"
                pipeline.waiting_for = "none"
                pipeline.status_reason = "Lead reopened by sales manager."
                pipeline.current_node = "crm_reopen_lead"
                pipeline.version = (pipeline.version or 0) + 1
            await record_business_event(
                session, business_id=user.business_id, customer_id=lead.customer_id,
                lead_id=lead.id, thread_id=lead.thread_id,
                event_type="crm.lead_reopened", source="crm", actor_type="employee",
                actor_id=user.user_id, entity_type="lead", entity_id=lead.id,
                data={"previous_lost_reason": previous_reason},
            )
            await session.commit()
        return await self.get_lead_payload(user, lead_id)

    async def timeline(self, user: AuthenticatedUser, lead_id: str) -> list[dict]:
        await self.get_lead_payload(user, lead_id)
        async with self.session_factory() as session:
            interactions = list((await session.scalars(select(Interaction).where(
                Interaction.business_id == user.business_id,
                Interaction.lead_id == lead_id,
            ))).all())
            events = list((await session.scalars(select(BusinessEvent).where(
                BusinessEvent.business_id == user.business_id,
                BusinessEvent.lead_id == lead_id,
            ))).all())
            activities = list((await session.scalars(select(CRMActivity).where(
                CRMActivity.business_id == user.business_id,
                CRMActivity.lead_id == lead_id,
            ))).all())
        items = [{
            "id": item.id, "kind": "interaction", "type": item.message_type,
            "summary": item.subject or item.content[:300], "occurred_at": item.occurred_at,
            "data": {"channel": item.channel, "direction": item.direction, "status": item.status},
        } for item in interactions]
        items.extend({
            "id": item.id, "kind": "business_event", "type": item.event_type,
            "summary": item.event_type, "occurred_at": item.occurred_at, "data": item.data,
        } for item in events)
        items.extend({
            "id": item.id, "kind": "crm_activity", "type": item.activity_type,
            "summary": item.subject, "occurred_at": item.occurred_at,
            "data": {"notes": item.notes, "outcome": item.outcome},
        } for item in activities)
        items.sort(key=lambda item: item["occurred_at"], reverse=True)
        for item in items:
            item["occurred_at"] = iso(item["occurred_at"])
        return items

    async def _require_member(
        self, session: AsyncSession, business_id: str, user_id: str,
    ) -> BusinessMembership:
        membership = await session.scalar(select(BusinessMembership).where(
            BusinessMembership.business_id == business_id,
            BusinessMembership.user_id == user_id,
            BusinessMembership.status == "active",
        ))
        if membership is None:
            raise HTTPException(status_code=422, detail="Assignee is not an active business member.")
        return membership

    @staticmethod
    def task_payload(task: CRMTask) -> dict:
        return {
            "id": task.id, "customer_id": task.customer_id, "lead_id": task.lead_id,
            "thread_id": task.thread_id, "assigned_to_user_id": task.assigned_to_user_id,
            "created_by_user_id": task.created_by_user_id, "task_type": task.task_type,
            "title": task.title, "description": task.description, "priority": task.priority,
            "status": task.status, "due_at": iso(task.due_at),
            "completed_at": iso(task.completed_at), "completion_notes": task.completion_notes,
            "version": task.version, "created_at": iso(task.created_at), "updated_at": iso(task.updated_at),
        }

    async def list_tasks(
        self, user: AuthenticatedUser, *, status: str | None, assigned_to: str | None,
        overdue: bool, page: int, page_size: int,
    ) -> dict:
        conditions = [CRMTask.business_id == user.business_id]
        if not user.has_permission("task:read_all"):
            conditions.append(CRMTask.assigned_to_user_id == user.user_id)
        if status:
            conditions.append(CRMTask.status == status)
        if assigned_to:
            conditions.append(CRMTask.assigned_to_user_id == (
                user.user_id if assigned_to == "me" else assigned_to
            ))
        if overdue:
            conditions.extend([
                CRMTask.status.in_(OPEN_TASK_STATUSES),
                CRMTask.due_at < utc_now(),
            ])
        async with self.session_factory() as session:
            total = await session.scalar(select(func.count()).select_from(CRMTask).where(*conditions)) or 0
            tasks = list((await session.scalars(
                select(CRMTask).where(*conditions).order_by(CRMTask.due_at)
                .offset((page - 1) * page_size).limit(page_size)
            )).all())
        return {"items": [self.task_payload(task) for task in tasks], "page": page, "page_size": page_size, "total": total}

    async def create_task(self, user: AuthenticatedUser, values: dict) -> dict:
        async with self.session_factory() as session:
            await self._require_member(session, user.business_id, values["assigned_to_user_id"])
            await self._validate_links(session, user.business_id, values.get("customer_id"), values.get("lead_id"))
            task = CRMTask(
                business_id=user.business_id,
                created_by_user_id=user.user_id,
                **values,
            )
            session.add(task)
            await session.flush()
            await record_business_event(
                session, business_id=user.business_id,
                customer_id=task.customer_id, lead_id=task.lead_id, thread_id=task.thread_id,
                event_type="crm.task_created", source="crm", actor_type="employee",
                actor_id=user.user_id, entity_type="crm_task", entity_id=task.id,
                data={"task_type": task.task_type, "assignee_id": task.assigned_to_user_id, "due_at": iso(task.due_at)},
            )
            await session.commit()
            return self.task_payload(task)

    async def update_task(
        self, user: AuthenticatedUser, task_id: str, changes: dict, expected_version: int,
    ) -> dict:
        async with self.session_factory() as session:
            task = await session.scalar(select(CRMTask).where(
                CRMTask.id == task_id, CRMTask.business_id == user.business_id,
            ).with_for_update())
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found.")
            self._authorize_task_edit(user, task)
            if task.version != expected_version:
                raise HTTPException(status_code=409, detail="Task was updated by another user. Refresh and retry.")
            if task.status not in OPEN_TASK_STATUSES:
                raise HTTPException(status_code=409, detail="Completed or cancelled task cannot be edited.")
            if "assigned_to_user_id" in changes:
                await self._require_member(session, user.business_id, changes["assigned_to_user_id"])
            for name, value in changes.items():
                setattr(task, name, value)
            task.version += 1
            await session.commit()
            return self.task_payload(task)

    async def finish_task(
        self, user: AuthenticatedUser, task_id: str, *, status: str,
        notes: str | None, expected_version: int,
    ) -> dict:
        now = utc_now()
        async with self.session_factory() as session:
            task = await session.scalar(select(CRMTask).where(
                CRMTask.id == task_id, CRMTask.business_id == user.business_id,
            ).with_for_update())
            if task is None:
                raise HTTPException(status_code=404, detail="Task not found.")
            self._authorize_task_edit(user, task)
            if task.version != expected_version:
                raise HTTPException(status_code=409, detail="Task was updated by another user. Refresh and retry.")
            if task.status not in OPEN_TASK_STATUSES:
                raise HTTPException(status_code=409, detail="Task is already finalized.")
            task.status = status
            task.completed_at = now if status == "completed" else None
            task.completion_notes = notes
            task.version += 1
            event_type = "crm.task_completed" if status == "completed" else "crm.task_cancelled"
            await record_business_event(
                session, business_id=user.business_id, customer_id=task.customer_id,
                lead_id=task.lead_id, thread_id=task.thread_id, event_type=event_type,
                source="crm", actor_type="employee", actor_id=user.user_id,
                entity_type="crm_task", entity_id=task.id, data={"notes": notes},
            )
            if status == "completed":
                session.add(CRMActivity(
                    business_id=user.business_id, customer_id=task.customer_id,
                    lead_id=task.lead_id, thread_id=task.thread_id, task_id=task.id,
                    activity_type="task_completed", subject=task.title,
                    notes=notes, actor_user_id=user.user_id, occurred_at=now,
                ))
            await session.commit()
            return self.task_payload(task)

    def _authorize_task_edit(self, user: AuthenticatedUser, task: CRMTask) -> None:
        allowed = user.has_permission("task:edit_all") or (
            user.has_permission("task:edit_assigned")
            and task.assigned_to_user_id == user.user_id
        )
        if not allowed:
            raise HTTPException(status_code=403, detail="Task edit permission denied.")

    async def _validate_links(
        self, session: AsyncSession, business_id: str,
        customer_id: str | None, lead_id: str | None,
    ) -> None:
        if customer_id and not await session.scalar(select(Customer.id).where(
            Customer.id == customer_id, Customer.business_id == business_id,
        )):
            raise HTTPException(status_code=422, detail="Customer does not belong to this business.")
        if lead_id and not await session.scalar(select(Lead.id).where(
            Lead.id == lead_id, Lead.business_id == business_id,
        )):
            raise HTTPException(status_code=422, detail="Lead does not belong to this business.")

    async def create_activity(self, user: AuthenticatedUser, values: dict) -> dict:
        async with self.session_factory() as session:
            await self._validate_links(session, user.business_id, values.get("customer_id"), values.get("lead_id"))
            activity = CRMActivity(
                business_id=user.business_id,
                actor_user_id=user.user_id,
                occurred_at=values.pop("occurred_at", None) or utc_now(),
                **values,
            )
            session.add(activity)
            await session.flush()
            await record_business_event(
                session, business_id=user.business_id,
                customer_id=activity.customer_id, lead_id=activity.lead_id,
                thread_id=activity.thread_id, event_type=f"crm.{activity.activity_type}",
                source="crm", actor_type="employee", actor_id=user.user_id,
                entity_type="crm_activity", entity_id=activity.id,
                data={"subject": activity.subject, "outcome": activity.outcome},
            )
            await session.commit()
            return {
                "id": activity.id, "activity_type": activity.activity_type,
                "subject": activity.subject, "notes": activity.notes,
                "outcome": activity.outcome, "occurred_at": iso(activity.occurred_at),
            }

    async def pipeline_cards(self, user: AuthenticatedUser) -> list[dict]:
        owner = aliased(User)
        conditions = [
            PipelineInstance.business_id == user.business_id,
            Lead.business_id == user.business_id,
        ]
        if not self._can_read_all(user):
            conditions.append(Lead.assigned_to_user_id == user.user_id)
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(PipelineInstance, Lead, Customer, owner.display_name)
                .join(Lead, and_(
                    Lead.id == PipelineInstance.lead_id,
                    Lead.business_id == PipelineInstance.business_id,
                ))
                .outerjoin(Customer, Customer.id == PipelineInstance.customer_id)
                .outerjoin(owner, owner.id == Lead.assigned_to_user_id)
                .where(*conditions)
                .order_by(PipelineInstance.updated_at)
            )).all()
        return [{
            "thread_id": pipeline.thread_id,
            "lead_id": lead.id,
            "customer_id": pipeline.customer_id,
            "customer_name": customer.company_name if customer else lead.company_name,
            "product": lead.product_requested,
            "quantity": lead.quantity,
            "owner_id": lead.assigned_to_user_id,
            "owner_name": owner_name,
            "pipeline_status": pipeline.pipeline_status,
            "business_milestone": pipeline.business_milestone,
            "waiting_for": pipeline.waiting_for,
            "status_reason": pipeline.status_reason,
            "current_node": pipeline.current_node,
            "updated_at": iso(pipeline.updated_at),
        } for pipeline, lead, customer, owner_name in rows]

    async def approvals(self, user: AuthenticatedUser) -> list[dict]:
        if not user.has_permission("approval:read"):
            raise HTTPException(status_code=403, detail="Approval read permission denied.")
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(PipelineInstance, Lead, Customer)
                .join(Lead, and_(Lead.id == PipelineInstance.lead_id, Lead.business_id == PipelineInstance.business_id))
                .outerjoin(Customer, Customer.id == PipelineInstance.customer_id)
                .where(
                    PipelineInstance.business_id == user.business_id,
                    PipelineInstance.pipeline_status == "awaiting_approval",
                )
                .order_by(PipelineInstance.updated_at)
            )).all()
        return [{
            "thread_id": pipeline.thread_id,
            "lead_id": lead.id,
            "customer_id": pipeline.customer_id,
            "customer_name": customer.company_name if customer else lead.company_name,
            "stage": pipeline.current_node,
            "approval_stage": pipeline.approval_stage,
            "waiting_for": pipeline.waiting_for,
            "reason": pipeline.status_reason,
            "updated_at": iso(pipeline.updated_at),
        } for pipeline, lead, customer in rows]

    async def customer_related(
        self, user: AuthenticatedUser, customer_id: str, resource: str,
    ) -> list[dict]:
        await self.get_customer(user, customer_id)
        model_map = {
            "interactions": (Interaction, Interaction.occurred_at),
            "quotations": (QuotationRecord, QuotationRecord.created_at),
            "orders": (SalesOrder, SalesOrder.created_at),
            "payments": (PaymentRecord, PaymentRecord.created_at),
            "pipelines": (PipelineInstance, PipelineInstance.updated_at),
            "followups": (FollowUpRecord, FollowUpRecord.created_at),
            "purchase_orders": (PurchaseOrder, PurchaseOrder.created_at),
        }
        model, order_column = model_map[resource]
        async with self.session_factory() as session:
            records = list((await session.scalars(
                select(model).where(
                    model.business_id == user.business_id,
                    model.customer_id == customer_id,
                ).order_by(order_column.desc()).limit(100)
            )).all())
        return [{column.name: enum_value(getattr(record, column.name)) for column in model.__table__.columns} for record in records]
