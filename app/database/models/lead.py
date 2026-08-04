import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class InquirySource(str, Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    WEBSITE = "website"
    INDIAMART = "indiamart"


class LeadStatus(str, Enum):
    AWAITING_INFO = "awaiting_info"
    NEW = "new"
    WON = "won"


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=True, index=True
    )
    thread_id: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )

    inquiry_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    source: Mapped[str] = mapped_column(
        SAEnum(InquirySource, name="inquiry_source_enum"),
    )

    sender_identifier: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    customer_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    company_name: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    contact_person: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    product_requested: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    quantity: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    specifications: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    delivery_location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    delivery_date: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    payment_expectation: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        SAEnum(LeadStatus, name="lead_status_enum"),
        default=LeadStatus.NEW,
    )

    missing_fields: Mapped[list] = mapped_column(
        JSON,
        default=list,
    )

    raw_text: Mapped[str] = mapped_column(
        Text,
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


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    business_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=True, index=True
    )
    thread_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True, index=True
    )

    entity_type: Mapped[str] = mapped_column(
        String(50),
    )

    entity_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    action: Mapped[str] = mapped_column(
        String(100),
    )

    actor: Mapped[str] = mapped_column(
        String(100),
    )

    details: Mapped[dict] = mapped_column(
        JSON,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
