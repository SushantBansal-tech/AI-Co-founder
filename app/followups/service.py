from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import (
    FollowUpJob,
    FollowUpJobStatus,
    Lead,
    QuotationRecord,
    QuotationStatus,
)


@dataclass(frozen=True)
class FollowUpScheduleItem:
    attempt_number: int
    days_after: int
    followup_type: str
    tone: str


FOLLOW_UP_SCHEDULE = (
    FollowUpScheduleItem(1, 3, "reminder_1", "gentle"),
    FollowUpScheduleItem(2, 7, "reminder_2", "moderate"),
    FollowUpScheduleItem(3, 14, "reminder_3", "urgent"),
    FollowUpScheduleItem(4, 25, "validity_expiry", "final"),
)

OPEN_JOB_STATUSES = (
    FollowUpJobStatus.SCHEDULED.value,
    FollowUpJobStatus.RETRY.value,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def schedule_quotation_followups(
    session: AsyncSession,
    *,
    business_id: str,
    customer_id: str | None,
    lead_id: str | None,
    thread_id: str,
    quotation_id: str,
    quotation_number: str,
    sent_at: datetime,
    channel: str,
    recipient: str,
    max_attempts: int = 5,
    created_by_principal_id: str | None = None,
) -> int:
    if not thread_id:
        raise ValueError("thread_id is required to schedule follow-ups.")
    if channel not in {"email", "whatsapp"}:
        raise ValueError(
            f"Unsupported follow-up channel: {channel}"
        )
    if not recipient:
        raise ValueError(
            "Follow-up recipient is required."
        )
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)

    values = []
    now = utc_now()
    for item in FOLLOW_UP_SCHEDULE:
        scheduled_for = sent_at + timedelta(
            days=item.days_after
        )
        values.append(
            {
                "business_id": business_id,
                "customer_id": customer_id,
                "lead_id": lead_id,
                "thread_id": thread_id,
                "quotation_id": quotation_id,
                "quotation_number": quotation_number,
                "attempt_number": item.attempt_number,
                "followup_type": item.followup_type,
                "tone": item.tone,
                "channel": channel,
                "recipient": recipient,
                "scheduled_for": scheduled_for,
                "next_attempt_at": scheduled_for,
                "status": FollowUpJobStatus.SCHEDULED.value,
                "attempt_count": 0,
                "max_attempts": max_attempts,
                "created_by_principal_id": created_by_principal_id,
                "created_at": now,
                "updated_at": now,
            }
        )

    bind = session.get_bind()
    dialect = bind.dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(FollowUpJob).values(
            values
        )
        statement = statement.on_conflict_do_nothing(
            constraint="uq_followup_job_quotation_attempt"
        )
    elif dialect == "sqlite":
        statement = sqlite_insert(FollowUpJob).values(values)
        statement = statement.on_conflict_do_nothing(
            index_elements=[
                "business_id",
                "quotation_id",
                "attempt_number",
            ]
        )
    else:
        existing_attempts = set(
            (
                await session.execute(
                    select(FollowUpJob.attempt_number).where(
                        FollowUpJob.business_id == business_id,
                        FollowUpJob.quotation_id == quotation_id,
                    )
                )
            ).scalars()
        )
        for value in values:
            if value["attempt_number"] not in existing_attempts:
                session.add(FollowUpJob(**value))
        await session.flush()
        return len(values) - len(existing_attempts)

    result = await session.execute(statement)
    return max(result.rowcount or 0, 0)


async def cancel_open_followup_jobs(
    session: AsyncSession,
    *,
    business_id: str,
    thread_id: str,
    reason: str,
) -> int:
    now = utc_now()
    result = await session.execute(
        update(FollowUpJob)
        .where(
            FollowUpJob.business_id == business_id,
            FollowUpJob.thread_id == thread_id,
            FollowUpJob.status.in_(OPEN_JOB_STATUSES),
        )
        .values(
            status=FollowUpJobStatus.CANCELLED.value,
            cancelled_at=now,
            cancellation_reason=reason,
            locked_at=None,
            locked_by=None,
            updated_at=now,
        )
    )
    return result.rowcount or 0


async def reconcile_followup_jobs(
    session: AsyncSession,
    *,
    business_id: str | None = None,
    max_attempts: int = 5,
) -> int:
    statement = select(QuotationRecord).where(
        QuotationRecord.status == QuotationStatus.SENT,
        QuotationRecord.sent_at.is_not(None),
        QuotationRecord.sent_via.in_(["email", "whatsapp"]),
        QuotationRecord.sent_to.is_not(None),
    )
    if business_id:
        statement = statement.where(
            QuotationRecord.business_id == business_id
        )

    quotations = (
        await session.execute(statement)
    ).scalars().all()
    created = 0
    for quotation in quotations:
        lead_id = await session.scalar(
            select(Lead.id).where(
                Lead.business_id == quotation.business_id,
                Lead.inquiry_id == quotation.inquiry_id,
            )
        )
        created += await schedule_quotation_followups(
            session,
            business_id=quotation.business_id,
            customer_id=quotation.customer_id,
            lead_id=lead_id,
            thread_id=quotation.thread_id,
            quotation_id=quotation.id,
            quotation_number=quotation.quotation_number,
            sent_at=quotation.sent_at,
            channel=quotation.sent_via,
            recipient=quotation.sent_to,
            max_attempts=max_attempts,
        )
    return created
