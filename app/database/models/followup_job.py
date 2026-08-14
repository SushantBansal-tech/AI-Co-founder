import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FollowUpJobStatus(str, Enum):
    SCHEDULED = "scheduled"
    PROCESSING = "processing"
    RETRY = "retry"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    DEAD = "dead"


class FollowUpJob(Base):
    __tablename__ = "followup_jobs"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "quotation_id",
            "attempt_number",
            name="uq_followup_job_quotation_attempt",
        ),
        Index(
            "ix_followup_jobs_due",
            "status",
            "next_attempt_at",
            "scheduled_for",
        ),
        Index(
            "ix_followup_jobs_tenant_status",
            "business_id",
            "status",
            "scheduled_for",
        ),
        Index(
            "ix_followup_jobs_thread",
            "business_id",
            "thread_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("customers.id"),
        nullable=True,
    )
    lead_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("leads.id"),
        nullable=True,
    )
    thread_id: Mapped[str] = mapped_column(
        String(100), nullable=False
    )
    quotation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("quotations.id"),
        nullable=False,
    )
    quotation_number: Mapped[str] = mapped_column(
        String(30), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False
    )
    followup_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )
    tone: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    channel: Mapped[str] = mapped_column(
        String(20), nullable=False
    )
    recipient: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(30),
        default=FollowUpJobStatus.SCHEDULED.value,
        nullable=False,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    locked_by: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    followup_record_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("followup_records.id"),
        nullable=True,
    )
    last_error: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancellation_reason: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    created_by_principal_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utc_now,
        onupdate=_utc_now,
        nullable=False,
    )
