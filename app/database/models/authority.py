import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class BusinessSettings(Base):
    __tablename__ = "business_settings"

    business_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    ai_operating_mode: Mapped[str] = mapped_column(
        String(40), nullable=False, default="recommend_only"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="INR")
    timezone: Mapped[str] = mapped_column(
        String(80), nullable=False, default="Asia/Kolkata"
    )
    maximum_automatic_discount_pct: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=Decimal("0")
    )
    maximum_automatic_quotation_value: Mapped[Decimal] = mapped_column(
        Numeric(18, 2), nullable=False, default=Decimal("0")
    )
    minimum_margin_pct: Mapped[Decimal] = mapped_column(
        Numeric(7, 4), nullable=False, default=Decimal("0")
    )
    daily_outbound_message_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=100
    )
    default_approval_role: Mapped[str] = mapped_column(
        String(40), nullable=False, default="admin"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    updated_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class BusinessSettingVersion(Base):
    __tablename__ = "business_setting_versions"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "version", name="uq_business_setting_version"
        ),
        Index(
            "ix_business_setting_versions_business_time",
            "business_id", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    settings_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now
    )


class AIServicePrincipal(Base):
    __tablename__ = "ai_service_principals"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "name", name="uq_ai_service_principal_name"
        ),
        UniqueConstraint("credential_hash", name="uq_ai_principal_credential_hash"),
        Index(
            "ix_ai_service_principals_business_status",
            "business_id", "status",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    principal_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="ai_agent"
    )
    credential_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    credential_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active")
    created_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rotated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AIPrincipalScope(Base):
    __tablename__ = "ai_principal_scopes"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "principal_id", "scope",
            name="uq_ai_principal_scope",
        ),
        Index(
            "ix_ai_principal_scopes_active",
            "business_id", "principal_id", "revoked_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    principal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(100), nullable=False)
    granted_by_user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now
    )
    revoked_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class AuthorityPolicy(Base):
    __tablename__ = "authority_policies"
    __table_args__ = (
        UniqueConstraint(
            "business_id", "action_type", name="uq_authority_policy_action"
        ),
        Index(
            "ix_authority_policies_business_enabled",
            "business_id", "enabled",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    active_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now, onupdate=utc_now
    )


class AuthorityPolicyVersion(Base):
    __tablename__ = "authority_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "policy_id", "version", name="uq_authority_policy_version"
        ),
        Index(
            "ix_authority_policy_versions_business_action",
            "business_id", "action_type", "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    policy_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("authority_policies.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_mode: Mapped[str] = mapped_column(String(40), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    required_scope: Mapped[str] = mapped_column(String(100), nullable=False)
    approval_role: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    conditions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    change_reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    effective_from: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=utc_now
    )


class AuthorityDecision(Base):
    """Immutable evidence ledger for a proposed Jarvis business action."""

    __tablename__ = "authority_decisions"
    __table_args__ = (
        Index("ix_authority_decisions_business_time", "business_id", "created_at"),
        Index(
            "ix_authority_decisions_business_action",
            "business_id", "action_type", "decision",
        ),
        Index("ix_authority_decisions_business_thread", "business_id", "thread_id"),
        Index(
            "ix_authority_decisions_business_entity",
            "business_id", "entity_type", "entity_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    principal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    tool_execution_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("ai_tool_executions.id"), nullable=True
    )
    thread_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(20), nullable=False)
    policy_code: Mapped[str] = mapped_column(String(100), nullable=False)
    policy_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("authority_policies.id"), nullable=True
    )
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    settings_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    approval_role: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    facts_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    reasons: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    missing_information: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    missing_master_data: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    evidence_chunk_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)


class AuthorityApprovalRequest(Base):
    """Approval bound to one decision, fact snapshot and policy version."""

    __tablename__ = "authority_approval_requests"
    __table_args__ = (
        UniqueConstraint("authority_decision_id", name="uq_authority_approval_decision"),
        Index(
            "ix_authority_approvals_business_status",
            "business_id", "status", "created_at",
        ),
        Index(
            "ix_authority_approvals_business_thread",
            "business_id", "thread_id",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    business_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authority_decision_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("authority_decisions.id"), nullable=False
    )
    action_type: Mapped[str] = mapped_column(String(80), nullable=False)
    entity_type: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    thread_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    requested_by_principal_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ai_service_principals.id"), nullable=False
    )
    required_role: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    policy_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("authority_policies.id"), nullable=True
    )
    policy_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    settings_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    facts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    approved_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    rejected_by_user_id: Mapped[Optional[str]] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=True
    )
    rejected_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    resolution_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=utc_now)
