import uuid
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AIToolExecution(Base):
    """Durable audit envelope for every controlled Jarvis tool invocation."""

    __tablename__ = "ai_tool_executions"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "principal_id", "tool_name", "idempotency_key",
            name="uq_ai_tool_execution_idempotency",
        ),
        Index(
            "ix_ai_tool_executions_business_time",
            "business_id", "started_at",
        ),
        Index(
            "ix_ai_tool_executions_principal_status",
            "business_id", "principal_id", "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    principal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    required_scope: Mapped[str] = mapped_column(String(100), nullable=False)
    is_mutation: Mapped[bool] = mapped_column(Boolean, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    authority_decision: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="processing")
    response_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
