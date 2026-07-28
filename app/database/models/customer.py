import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    Float,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
    relationship,
)

from app.database.base import Base


# ---------------------------------------------------------------------------
# Database-related enums
# ---------------------------------------------------------------------------

class PaymentBehavior(str, Enum):
    EXCELLENT = "excellent"
    GOOD = "good"
    AVERAGE = "average"
    POOR = "poor"
    UNKNOWN = "unknown"


class OrderStatus(str, Enum):
    DELIVERED = "delivered"
    IN_TRANSIT = "in_transit"
    CANCELLED = "cancelled"
    PENDING = "pending"


class QuotationStatus(str, Enum):
    WON = "won"
    LOST = "lost"
    PENDING = "pending"
    EXPIRED = "expired"


class CustomerMatchReviewStatus(str, Enum):
    PENDING = "pending"
    MERGED = "merged"
    KEPT_SEPARATE = "kept_separate"
    DISMISSED = "dismissed"


# ---------------------------------------------------------------------------
# Customer
# ---------------------------------------------------------------------------

class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )

    status: Mapped[str] = mapped_column(
        String(30), default="active", nullable=False, index=True
    )

    merged_into_customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=True, index=True
    )

    company_name: Mapped[str] = mapped_column(
        String(255),
        index=True,
    )

    contact_person: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    phone: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    city: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    gstin: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
    )

    credit_limit: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        default=0,
    )

    outstanding_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
        default=0,
    )

    payment_behavior: Mapped[str] = mapped_column(
        SAEnum(PaymentBehavior, name="payment_behavior_enum"),
        default=PaymentBehavior.UNKNOWN,
    )

    notes: Mapped[Optional[str]] = mapped_column(
        Text,
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

    orders: Mapped[list["OrderHistory"]] = relationship(
        "OrderHistory",
        back_populates="customer",
        lazy="select",
    )

    quotations: Mapped[list["QuotationHistory"]] = relationship(
        "QuotationHistory",
        back_populates="customer",
        lazy="select",
    )

    payments: Mapped[list["PaymentRecord"]] = relationship(
        "PaymentRecord",
        back_populates="customer",
        lazy="select",
    )


class CustomerIdentity(Base):
    __tablename__ = "customer_identities"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "identity_type",
            "normalized_value",
            name="uq_customer_identity",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=False, index=True
    )
    identity_type: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True
    )
    raw_value: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_value: Mapped[str] = mapped_column(
        String(255), nullable=False, index=True
    )
    is_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    source: Mapped[str] = mapped_column(
        String(50), default="inquiry", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


class CustomerMatchReview(Base):
    __tablename__ = "customer_match_reviews"

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    lead_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("leads.id"), nullable=True, index=True
    )
    provisional_customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=False, index=True
    )
    candidate_customer_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=False, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    matched_signals: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False
    )
    conflicting_signals: Mapped[list] = mapped_column(
        JSON, default=list, nullable=False
    )
    status: Mapped[str] = mapped_column(
        SAEnum(
            CustomerMatchReviewStatus,
            name="customer_match_review_status_enum",
        ),
        default=CustomerMatchReviewStatus.PENDING,
        nullable=False,
        index=True,
    )
    resolved_by: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    resolution_notes: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )


# ---------------------------------------------------------------------------
# Order history
# ---------------------------------------------------------------------------

class OrderHistory(Base):
    __tablename__ = "order_history"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )

    customer_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("customers.id"),
        index=True,
    )

    order_number: Mapped[str] = mapped_column(
        String(50),
    )

    product: Mapped[str] = mapped_column(
        String(255),
    )

    quantity: Mapped[str] = mapped_column(
        String(100),
    )

    order_value: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
    )

    status: Mapped[str] = mapped_column(
        SAEnum(OrderStatus,name="order_history_status_enum"),
    )

    order_date: Mapped[date] = mapped_column(
        Date,
    )

    customer: Mapped["Customer"] = relationship(
        "Customer",
        back_populates="orders",
    )


# ---------------------------------------------------------------------------
# Quotation history
# ---------------------------------------------------------------------------

class QuotationHistory(Base):
    __tablename__ = "quotation_history"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )

    customer_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("customers.id"),
        index=True,
    )

    quotation_number: Mapped[str] = mapped_column(
        String(50),
    )

    product: Mapped[str] = mapped_column(
        String(255),
    )

    quoted_value: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
    )

    status: Mapped[str] = mapped_column(
        SAEnum(QuotationStatus, name="quotation_history_status_enum"),
    )

    quotation_date: Mapped[date] = mapped_column(
        Date,
    )

    lost_reason: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    customer: Mapped["Customer"] = relationship(
        "Customer",
        back_populates="quotations",
    )


# ---------------------------------------------------------------------------
# Payment records
# ---------------------------------------------------------------------------

class PaymentRecord(Base):
    __tablename__ = "payment_records"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )

    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )

    customer_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("customers.id"),
        index=True,
    )

    invoice_number: Mapped[str] = mapped_column(
        String(50),
    )

    invoice_amount: Mapped[Decimal] = mapped_column(
        Numeric(15, 2),
    )

    due_date: Mapped[date] = mapped_column(
        Date,
    )

    paid_date: Mapped[Optional[date]] = mapped_column(
        Date,
        nullable=True,
    )

    delay_days: Mapped[int] = mapped_column(
        Integer,
        default=0,
    )

    customer: Mapped["Customer"] = relationship(
        "Customer",
        back_populates="payments",
    )
