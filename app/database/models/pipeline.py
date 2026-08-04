import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PipelineInstance(Base):
    __tablename__ = "pipeline_instances"
    __table_args__ = (
        UniqueConstraint("business_id", "thread_id", name="uq_pipeline_instance_thread"),
        Index("ix_pipeline_instances_status", "business_id", "pipeline_status", "updated_at"),
        Index("ix_pipeline_instances_waiting", "business_id", "waiting_for", "updated_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("customers.id"), nullable=True)
    lead_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("leads.id"), nullable=True)
    pipeline_status: Mapped[str] = mapped_column(String(60), nullable=False, default="processing")
    business_milestone: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    waiting_for: Mapped[str] = mapped_column(String(60), nullable=False, default="none")
    status_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    current_node: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    failure_category: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    failure_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    failure_details: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class QuotationDeliveryAttempt(Base):
    __tablename__ = "quotation_delivery_attempts"
    __table_args__ = (
        UniqueConstraint("business_id", "quotation_id", "channel", "recipient", name="uq_quotation_delivery_target"),
        Index("ix_quotation_delivery_status", "business_id", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    quotation_id: Mapped[str] = mapped_column(String(36), ForeignKey("quotations.id"), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(100), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    recipient: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="prepared")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    provider_message_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class InventoryReservation(Base):
    __tablename__ = "inventory_reservations"
    __table_args__ = (
        UniqueConstraint("business_id", "po_id", "inventory_record_id", name="uq_inventory_reservation_po_row"),
        Index("ix_inventory_reservations_product", "business_id", "product_code", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("customers.id"), nullable=True)
    po_id: Mapped[str] = mapped_column(String(36), ForeignKey("purchase_orders.id"), nullable=False)
    inventory_record_id: Mapped[str] = mapped_column(String(36), ForeignKey("inventory_records.id"), nullable=False)
    product_code: Mapped[str] = mapped_column(String(80), nullable=False)
    quantity: Mapped[float] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="reserved")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
