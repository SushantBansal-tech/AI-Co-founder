import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    JSON,
    Index,
    String,
    Text,
)
from sqlalchemy.orm import (
    Mapped,
    mapped_column,
)

from app.database.base import Base


# ---------------------------------------------------------------------------
# Purchase-order lifecycle status
#
# Kept as a string constants class to preserve the existing behavior.
# It can become an Enum in a later schema migration.
# ---------------------------------------------------------------------------

class POStatus(str):
    PENDING = "pending"
    VALID = "valid"
    MISMATCH = "mismatch_found"
    CORRECTED = "corrected"
    CONFIRMED = "confirmed"


# ---------------------------------------------------------------------------
# Purchase order
# ---------------------------------------------------------------------------

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

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

    inquiry_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
        index=True,
    )

    quotation_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        nullable=True,
    )

    quotation_number: Mapped[Optional[str]] = mapped_column(
        String(30),
        nullable=True,
    )

    po_number: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    po_date: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    buyer_company: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    buyer_gstin: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
    )

    billing_address: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    shipping_address: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    product_description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    product_code: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    quantity: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    unit: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
    )

    price_per_unit_ex_gst: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    gst_rate_pct: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    gst_amount: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    total_amount_inc_gst: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    payment_terms: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    delivery_date: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    delivery_location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    special_conditions: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default=POStatus.PENDING,
    )

    mismatches_json: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
    )

    missing_critical_fields: Mapped[Optional[list]] = mapped_column(
        JSON,
        nullable=True,
    )

    extraction_confidence: Mapped[float] = mapped_column(
        Float,
        default=0.0,
    )

    raw_po_text: Mapped[str] = mapped_column(
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


# ---------------------------------------------------------------------------
# Sales order
# ---------------------------------------------------------------------------

class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        Index("ix_sales_orders_business_created", "business_id", "created_at"),
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

    inquiry_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    quotation_id: Mapped[str] = mapped_column(
        String(36),
    )

    po_id: Mapped[str] = mapped_column(
        String(36),
        index=True,
    )

    po_number: Mapped[str] = mapped_column(
        String(100),
    )

    buyer_company: Mapped[str] = mapped_column(
        String(255),
    )

    product_code: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    product_description: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    quantity: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    unit: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
    )

    total_value: Mapped[Optional[float]] = mapped_column(
        Float,
        nullable=True,
    )

    delivery_date: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    delivery_location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    payment_terms: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    special_notes: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(30),
        default="confirmed",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )
