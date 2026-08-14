from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.crm import BusinessMembership, LeadAssignment, User
from app.database.models.customer import Customer
from app.database.models.lead import Lead
from app.events.service import record_business_event


async def auto_assign_lead(
    session: AsyncSession,
    *,
    business_id: str,
    lead_id: str,
) -> str | None:
    """Assign deterministically without changing the sales decision workflow.

    Existing account ownership wins. New accounts use the active salesperson
    with the fewest currently assigned leads, with user ID as a stable tie-break.
    """
    lead = await session.scalar(
        select(Lead).where(
            Lead.id == lead_id,
            Lead.business_id == business_id,
        ).with_for_update()
    )
    if lead is None or lead.assigned_to_user_id:
        return lead.assigned_to_user_id if lead else None

    assignee_id = None
    reason = None
    customer = None
    if lead.customer_id:
        customer = await session.scalar(
            select(Customer).where(
                Customer.id == lead.customer_id,
                Customer.business_id == business_id,
            )
        )
        if customer and customer.account_owner_id:
            active_owner = await session.scalar(
                select(BusinessMembership.id).where(
                    BusinessMembership.business_id == business_id,
                    BusinessMembership.user_id == customer.account_owner_id,
                    BusinessMembership.status == "active",
                )
            )
            if active_owner:
                assignee_id = customer.account_owner_id
                reason = "existing_customer_account_owner"

    if assignee_id is None:
        rows = (
            await session.execute(
                select(User.id, func.count(Lead.id).label("active_leads"))
                .join(BusinessMembership, BusinessMembership.user_id == User.id)
                .outerjoin(
                    Lead,
                    (Lead.assigned_to_user_id == User.id)
                    & (Lead.business_id == business_id)
                    & (Lead.closed_lost_at.is_(None)),
                )
                .where(
                    BusinessMembership.business_id == business_id,
                    BusinessMembership.role == "salesperson",
                    BusinessMembership.status == "active",
                    User.status == "active",
                )
                .group_by(User.id)
                .order_by(func.count(Lead.id), User.id)
                .limit(1)
            )
        ).one_or_none()
        if rows:
            assignee_id = rows[0]
            reason = "round_robin_lowest_open_lead_count"

    if assignee_id is None:
        return None

    now = datetime.now(UTC).replace(tzinfo=None)
    lead.assigned_to_user_id = assignee_id
    lead.assigned_at = now
    lead.assigned_by_user_id = assignee_id
    if customer is not None and customer.account_owner_id is None:
        customer.account_owner_id = assignee_id
    session.add(LeadAssignment(
        business_id=business_id,
        lead_id=lead.id,
        assigned_to_user_id=assignee_id,
        assigned_by_user_id=assignee_id,
        reason=reason,
        started_at=now,
    ))
    await record_business_event(
        session,
        business_id=business_id,
        customer_id=lead.customer_id,
        lead_id=lead.id,
        thread_id=lead.thread_id,
        event_type="crm.lead_auto_assigned",
        source="crm",
        actor_type="system",
        actor_id="deterministic_assignment",
        entity_type="lead",
        entity_id=lead.id,
        data={"assignee_id": assignee_id, "reason": reason},
    )
    return assignee_id
