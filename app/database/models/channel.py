import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ChannelSource(Base):
    __tablename__ = "channel_sources"
    __table_args__ = (
        UniqueConstraint(
            "public_key",
            name="uq_channel_sources_public_key",
        ),
        Index(
            "ix_channel_sources_business_channel_active",
            "business_id",
            "channel",
            "active",
        ),
        UniqueConstraint(
            "channel",
            "provider",
            "provider_account_id",
            name="uq_channel_source_provider_account",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
    )
    business_id: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_account_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True, index=True
    )
    public_key: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    configuration: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    last_poll_started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_poll_completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_successful_poll_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    last_seen_uid: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    last_poll_messages_found: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    last_poll_messages_enqueued: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    last_poll_error: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )


class ChannelConversation(Base):
    __tablename__ = "channel_conversations"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "thread_id", "channel",
            name="uq_channel_conversation_thread_channel",
        ),
        UniqueConstraint(
            "business_id", "channel", "external_conversation_id",
            name="uq_channel_conversation_external",
        ),
        Index(
            "ix_channel_conversations_participant_status",
            "business_id", "participant_identifier", "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=True
    )
    lead_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("leads.id"), nullable=True
    )
    thread_id: Mapped[str] = mapped_column(String(100), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    channel_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channel_sources.id"), nullable=False
    )
    participant_identifier: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    external_conversation_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    root_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    latest_message_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30), default="active", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow,
        nullable=False,
    )


class ChannelIngestion(Base):
    __tablename__ = "channel_ingestions"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "channel",
            "provider",
            "external_event_id",
            name="uq_channel_ingestion_external_event",
        ),
        Index(
            "ix_channel_ingestions_business_status_received",
            "business_id",
            "status",
            "received_at",
        ),
        Index(
            "ix_channel_ingestions_thread",
            "business_id",
            "thread_id",
        ),
        Index(
            "ix_channel_ingestions_source_received",
            "channel_source_id",
            "received_at",
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
    channel_source_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("channel_sources.id"),
        nullable=False,
    )
    interaction_id: Mapped[Optional[str]] = mapped_column(
        String(36),
        ForeignKey("interactions.id"),
        nullable=True,
    )
    thread_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_event_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False
    )
    normalized_payload: Mapped[Optional[dict]] = mapped_column(
        JSON, nullable=True
    )
    error_message: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )


class ChannelCursor(Base):
    __tablename__ = "channel_cursors"
    __table_args__ = (
        UniqueConstraint(
            "channel_source_id",
            "cursor_type",
            name="uq_channel_cursor_source_type",
        ),
        Index(
            "ix_channel_cursors_business_source",
            "business_id",
            "channel_source_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channel_sources.id"), nullable=False
    )
    cursor_type: Mapped[str] = mapped_column(String(50), nullable=False)
    cursor_value: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    metadata_json: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class ChannelInboundJob(Base):
    __tablename__ = "channel_inbound_jobs"
    __table_args__ = (
        UniqueConstraint(
            "business_id",
            "channel",
            "provider",
            "external_event_id",
            name="uq_channel_inbound_job_external_event",
        ),
        Index(
            "ix_channel_inbound_jobs_due",
            "status",
            "next_attempt_at",
            "created_at",
        ),
        Index(
            "ix_channel_inbound_jobs_tenant_status",
            "business_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    channel_source_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channel_sources.id"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_event_id: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False
    )
    normalized_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    raw_payload: Mapped[dict] = mapped_column(
        JSON, default=dict, nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, default=5, nullable=False
    )
    locked_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    locked_by: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )
    ingestion_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("channel_ingestions.id"), nullable=True
    )
    thread_id: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime, nullable=True
    )


class ChannelAttachment(Base):
    __tablename__ = "channel_attachments"
    __table_args__ = (
        Index(
            "ix_channel_attachments_business_job",
            "business_id",
            "inbound_job_id",
        ),
        UniqueConstraint(
            "inbound_job_id",
            "checksum_sha256",
            name="uq_channel_attachment_job_checksum",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    inbound_job_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("channel_inbound_jobs.id"), nullable=False
    )
    provider_file_id: Mapped[Optional[str]] = mapped_column(
        String(255), nullable=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[Optional[str]] = mapped_column(
        String(100), nullable=True
    )
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    scan_status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
