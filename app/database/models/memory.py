import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CustomerNote(Base):
    """Authoritative, tenant-scoped customer memory stored in PostgreSQL."""

    __tablename__ = "customer_notes"
    __table_args__ = (
        UniqueConstraint("business_id", "request_event_id", name="uq_customer_note_request_event"),
        Index("ix_customer_notes_customer_time", "business_id", "customer_id", "occurred_at"),
        Index("ix_customer_notes_thread", "business_id", "thread_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False)
    thread_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    interaction_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("interactions.id"), nullable=True)
    request_event_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(100), nullable=False, default="api")
    created_by_principal_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)


class MemoryOutbox(Base):
    """Durable PostgreSQL queue for projecting customer notes into Qdrant."""

    __tablename__ = "memory_outbox"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "source_type", "source_id", "memory_type",
            name="uq_memory_outbox_source",
        ),
        Index("ix_memory_outbox_due", "status", "next_attempt_at"),
        Index("ix_memory_outbox_customer", "business_id", "customer_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_id: Mapped[str] = mapped_column(String(100), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    thread_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    interaction_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now)
