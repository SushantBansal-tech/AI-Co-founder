from datetime import datetime
from typing import Literal, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.customer import (
    Customer,
    CustomerIdentity,
    CustomerMatchReview,
    CustomerMatchReviewStatus,
    OrderHistory,
    PaymentRecord,
    QuotationHistory,
)
from app.database.models.activity import BusinessEvent, Interaction
from app.database.models.followup import FollowUpRecord
from app.database.models.followup_job import FollowUpJob
from app.database.models.handoff import HandoffRecord
from app.database.models.lead import AuditLog, Lead
from app.database.models.memory import CustomerNote, MemoryOutbox
from app.database.models.order import PurchaseOrder, SalesOrder
from app.database.models.pipeline import InventoryReservation, PipelineInstance
from app.database.models.quotation import QuotationRecord, QuotationVersion
from app.database.models.channel import ChannelConversation
from app.database.models.crm import CRMActivity, CRMTask
from app.database.models.structured import CustomerImportStaging
from app.events.service import record_business_event


LIFECYCLE_MODELS = (
    Lead,
    OrderHistory,
    QuotationHistory,
    PaymentRecord,
    QuotationRecord,
    QuotationVersion,
    FollowUpRecord,
    PurchaseOrder,
    SalesOrder,
    HandoffRecord,
    AuditLog,
    Interaction,
    BusinessEvent,
    PipelineInstance,
    ChannelConversation,
    FollowUpJob,
    InventoryReservation,
    CustomerNote,
    MemoryOutbox,
    CRMTask,
    CRMActivity,
)


async def _merge_customers(
    session: AsyncSession,
    source: Customer,
    target: Customer,
) -> None:
    identities = (
        await session.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.customer_id == source.id
            )
        )
    ).scalars().all()

    for identity in identities:
        duplicate = await session.scalar(
            select(CustomerIdentity).where(
                CustomerIdentity.business_id == target.business_id,
                CustomerIdentity.identity_type == identity.identity_type,
                CustomerIdentity.normalized_value == identity.normalized_value,
                CustomerIdentity.customer_id == target.id,
            )
        )
        if duplicate:
            await session.delete(identity)
        else:
            identity.customer_id = target.id

    for model in LIFECYCLE_MODELS:
        await session.execute(
            update(model)
            .where(model.customer_id == source.id)
            .values(customer_id=target.id)
        )

    await session.execute(
        update(CustomerImportStaging)
        .where(
            CustomerImportStaging.business_id == source.business_id,
            CustomerImportStaging.resolved_customer_id == source.id,
        )
        .values(resolved_customer_id=target.id)
    )
    await session.execute(
        update(Customer)
        .where(
            Customer.business_id == source.business_id,
            Customer.merged_into_customer_id == source.id,
        )
        .values(merged_into_customer_id=target.id)
    )

    source.status = "merged"
    source.merged_into_customer_id = target.id


async def resolve_customer_match_review(
    session: AsyncSession,
    *,
    review_id: str,
    business_id: str,
    action: Literal["merge", "keep_separate", "dismiss"],
    resolved_by: str,
    notes: Optional[str] = None,
) -> CustomerMatchReview:
    review = await session.scalar(
        select(CustomerMatchReview).where(
            CustomerMatchReview.id == review_id,
            CustomerMatchReview.business_id == business_id,
        ).with_for_update()
    )
    if review is None:
        raise ValueError("Customer match review not found.")
    if review.status != CustomerMatchReviewStatus.PENDING:
        raise ValueError("Customer match review is already resolved.")

    provisional = await session.scalar(
        select(Customer).where(
            Customer.id == review.provisional_customer_id,
            Customer.business_id == business_id,
        ).with_for_update()
    )
    candidate = await session.scalar(
        select(Customer).where(
            Customer.id == review.candidate_customer_id,
            Customer.business_id == business_id,
        ).with_for_update()
    )
    if not provisional or not candidate:
        raise ValueError("Customer match review references a missing customer.")

    if action == "merge":
        await _merge_customers(session, provisional, candidate)
        review.status = CustomerMatchReviewStatus.MERGED
        await record_business_event(
            session,
            business_id=business_id,
            customer_id=candidate.id,
            lead_id=review.lead_id,
            event_type="crm.customer_merged",
            source="crm",
            actor_type="employee",
            actor_id=resolved_by,
            entity_type="customer",
            entity_id=candidate.id,
            data={
                "source_customer_id": provisional.id,
                "target_customer_id": candidate.id,
                "review_id": review.id,
                "matched_signals": review.matched_signals,
                "confidence": review.confidence,
                "resolution_notes": notes,
            },
        )
    elif action == "keep_separate":
        provisional.status = "active"
        review.status = CustomerMatchReviewStatus.KEPT_SEPARATE
    else:
        review.status = CustomerMatchReviewStatus.DISMISSED

    review.resolved_by = resolved_by
    review.resolution_notes = notes
    review.resolved_at = datetime.utcnow()
    await session.commit()
    return review
