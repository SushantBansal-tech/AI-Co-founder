import uuid
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class JarvisConversation(Base):
    __tablename__ = "jarvis_conversations"
    __table_args__ = (
        Index(
            "ix_jarvis_conversations_business_user_time",
            "business_id", "created_by_user_id", "updated_at",
        ),
        Index("ix_jarvis_conversations_business_status", "business_id", "status"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class JarvisMessage(Base):
    __tablename__ = "jarvis_messages"
    __table_args__ = (
        Index(
            "ix_jarvis_messages_business_conversation_time",
            "business_id", "conversation_id", "created_at",
        ),
        Index("ix_jarvis_messages_action", "action_request_id"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jarvis_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_call_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    action_request_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ai_action_requests.id"), nullable=True
    )
    supporting_data: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


class JarvisRun(Base):
    __tablename__ = "jarvis_runs"
    __table_args__ = (
        Index("ix_jarvis_runs_business_status_time", "business_id", "status", "started_at"),
        Index("ix_jarvis_runs_conversation_time", "conversation_id", "started_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jarvis_conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    requested_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    principal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PLANNING")
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("jarvis_messages.id"), nullable=False
    )
    final_message_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("jarvis_messages.id"), nullable=True
    )
    plan_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    tool_results_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    supporting_data: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
