import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
)

from app.database.base import Base


# ---------------------------------------------------------------------------
# Shared quotation status
#
# Used by:
# - QuotationDraft Pydantic model
# - QuotationRecord database model
# - Quotation workflow functions
# ---------------------------------------------------------------------------

class QuotationStatus(str, Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SENT = "sent"
    REJECTED = "rejected"


# ---------------------------------------------------------------------------
# Current quotation record
# ---------------------------------------------------------------------------

class QuotationRecord(Base):
    __tablename__ = "quotations"

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

    quotation_number: Mapped[str] = mapped_column(
        String(30),
        unique=True,
        index=True,
    )

    inquiry_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    status: Mapped[str] = mapped_column(
        SAEnum(QuotationStatus, name="quotation_workflow_status_enum"),
    )

    buyer_company: Mapped[str] = mapped_column(
        String(255),
    )

    total_inc_gst: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    requires_approval: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    draft_json: Mapped[str] = mapped_column(
        Text,
    )

    html_content: Mapped[str] = mapped_column(
        Text,
    )

    approved_by: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    rejection_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    sent_via: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    sent_to: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
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


# ---------------------------------------------------------------------------
# Quotation version history
# ---------------------------------------------------------------------------

class QuotationVersion(Base):
    __tablename__ = "quotation_versions"

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

    version_number: Mapped[int] = mapped_column(
        Integer,
    )

    price_per_mt_ex_gst: Mapped[float] = mapped_column(
        Float,
    )

    discount_pct: Mapped[float] = mapped_column(
        Float,
    )

    subtotal_ex_gst: Mapped[float] = mapped_column(
        Float,
    )

    gst_amount: Mapped[float] = mapped_column(
        Float,
    )

    total_inc_gst: Mapped[float] = mapped_column(
        Float,
    )

    change_reason: Mapped[str] = mapped_column(
        String(255),
    )

    changed_by: Mapped[str] = mapped_column(
        String(100),
    )

    negotiation_decision: Mapped[Optional[str]] = mapped_column(
        String(30),
        nullable=True,
    )

    customer_offered_price: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    draft_json: Mapped[str] = mapped_column(
        Text,
    )

    html_content: Mapped[str] = mapped_column(
        Text,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
