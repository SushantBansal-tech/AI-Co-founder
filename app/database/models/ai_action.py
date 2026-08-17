import uuid
from datetime import UTC, datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class AIActionRequest(Base):
    """Durable aggregate for one business action proposed by Jarvis."""

    __tablename__ = "ai_action_requests"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "principal_id", "tool_name", "idempotency_key",
            name="uq_ai_action_request_idempotency",
        ),
        Index("ix_ai_actions_business_status", "business_id", "status", "proposed_at"),
        Index("ix_ai_actions_business_customer", "business_id", "customer_id", "proposed_at"),
        Index("ix_ai_actions_business_thread", "business_id", "thread_id", "proposed_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    principal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=False
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="PROPOSED")
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    arguments_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    customer_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("customers.id"), nullable=True
    )
    lead_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("leads.id"), nullable=True
    )
    thread_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    latest_authority_decision_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("authority_decisions.id"), nullable=True
    )
    active_approval_request_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("authority_approval_requests.id"), nullable=True
    )
    latest_tool_execution_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ai_tool_executions.id"), nullable=True
    )
    policy_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    settings_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approval_role: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    evaluated_facts_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    execution_result_json: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    error_code: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    execution_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    proposed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
    evaluated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    execution_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class ApprovalDecision(Base):
    """Append-only human decision for an authority approval request."""

    __tablename__ = "approval_decisions"
    __table_args__ = (
        UniqueConstraint("approval_request_id", name="uq_approval_decision_request"),
        Index("ix_approval_decisions_business_time", "business_id", "decided_at"),
        Index("ix_approval_decisions_action", "action_request_id", "decided_at"),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action_request_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ai_action_requests.id"), nullable=True
    )
    approval_request_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("authority_approval_requests.id"), nullable=False
    )
    authority_decision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("authority_decisions.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    decided_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    decided_by_role: Mapped[str] = mapped_column(String(40), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    settings_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    facts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
