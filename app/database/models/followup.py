import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
)

from app.database.base import Base


class FollowUpStatus(str, Enum):
    SCHEDULED = "scheduled"
    SENT = "sent"
    CUSTOMER_REPLIED = "customer_replied"
    OBJECTION_DETECTED = "objection_detected"
    NEGOTIATION_ACTIVE = "negotiation_active"
    CLOSED_WON = "closed_won"
    CLOSED_LOST = "closed_lost"
    EXPIRED = "expired"


class FollowUpType(str, Enum):
    REMINDER_1 = "reminder_1"
    REMINDER_2 = "reminder_2"
    REMINDER_3 = "reminder_3"
    VALIDITY_EXPIRY = "validity_expiry"
    OBJECTION_RESPONSE = "objection_response"
    NEGOTIATION_FOLLOWUP = "negotiation_followup"


class FollowUpRecord(Base):
    __tablename__ = "followup_records"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "quotation_id",
            "attempt_number",
            name="uq_followup_record_quotation_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    business_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)

    quotation_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    quotation_number: Mapped[str] = mapped_column(
        String(30),
        index=True,
    )

    inquiry_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    buyer_company: Mapped[str] = mapped_column(
        String(255),
    )

    channel: Mapped[str] = mapped_column(
        String(20),
    )

    recipient: Mapped[str] = mapped_column(
        String(255),
    )

    attempt_number: Mapped[int] = mapped_column(
        Integer,
        default=1,
    )

    followup_type: Mapped[str] = mapped_column(
        SAEnum(FollowUpType, name="followup_type_enum"),
    )

    tone: Mapped[str] = mapped_column(
        String(20),
        default="gentle",
    )

    message_text: Mapped[str] = mapped_column(
        Text,
    )

    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        SAEnum(FollowUpStatus, name="followup_status_enum"),
        default=FollowUpStatus.SCHEDULED,
    )

    customer_reply: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    reply_received_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    objection_type: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
