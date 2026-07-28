import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    DateTime,
    ForeignKey,
    String,
    Text,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
)

from app.database.base import Base


class HandoffRecordStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class HandoffRecord(Base):
    __tablename__ = "handoff_records"

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

    handoff_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    sales_order_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    po_number: Mapped[str] = mapped_column(
        String(100),
    )

    quotation_number: Mapped[str] = mapped_column(
        String(30),
    )

    buyer_company: Mapped[str] = mapped_column(
        String(255),
    )

    department: Mapped[str] = mapped_column(
        String(30),
        index=True,
    )

    priority: Mapped[str] = mapped_column(
        String(5),
    )

    job_reference: Mapped[str] = mapped_column(
        String(50),
    )

    subject: Mapped[str] = mapped_column(
        String(255),
    )

    package_json: Mapped[str] = mapped_column(
        Text,
    )

    notification_channel: Mapped[str] = mapped_column(
        String(50),
        default="email",
    )

    notification_recipient: Mapped[str] = mapped_column(
        String(255),
        default="",
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default=HandoffRecordStatus.PENDING,
    )

    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    acknowledged_by: Mapped[Optional[str]] = mapped_column(
        String(100),
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
