"""add business AI authority control plane

Revision ID: a19e4d6b2c71
Revises: e51c9a7d2f40
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a19e4d6b2c71"
down_revision: Union[str, Sequence[str], None] = "e51c9a7d2f40"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_settings",
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("ai_operating_mode", sa.String(40), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False),
        sa.Column("maximum_automatic_discount_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("maximum_automatic_quotation_value", sa.Numeric(18, 2), nullable=False),
        sa.Column("minimum_margin_pct", sa.Numeric(7, 4), nullable=False),
        sa.Column("daily_outbound_message_limit", sa.Integer(), nullable=False),
        sa.Column("default_approval_role", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
        sa.Column("updated_by_user_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("business_id"),
    )
    op.create_table(
        "business_setting_versions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("settings_snapshot", sa.JSON(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "version", name="uq_business_setting_version"),
    )
    op.create_index(
        "ix_business_setting_versions_business_time",
        "business_setting_versions", ["business_id", "created_at"],
    )
    op.create_table(
        "ai_service_principals",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("principal_type", sa.String(30), nullable=False),
        sa.Column("credential_prefix", sa.String(24), nullable=False),
        sa.Column("credential_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("rotated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "name", name="uq_ai_service_principal_name"),
        sa.UniqueConstraint("credential_hash", name="uq_ai_principal_credential_hash"),
    )
    op.create_index(
        "ix_ai_service_principals_business_status",
        "ai_service_principals", ["business_id", "status"],
    )
    op.create_table(
        "ai_principal_scopes",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.Column("scope", sa.String(100), nullable=False),
        sa.Column("granted_by_user_id", sa.String(36), nullable=False),
        sa.Column("granted_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_by_user_id", sa.String(36), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["principal_id"], ["ai_service_principals.id"]),
        sa.ForeignKeyConstraint(["granted_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["revoked_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id", "principal_id", "scope", name="uq_ai_principal_scope"
        ),
    )
    op.create_index(
        "ix_ai_principal_scopes_active", "ai_principal_scopes",
        ["business_id", "principal_id", "revoked_at"],
    )
    op.create_table(
        "authority_policies",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("name", sa.String(160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("active_version", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "action_type", name="uq_authority_policy_action"),
    )
    op.create_index(
        "ix_authority_policies_business_enabled", "authority_policies",
        ["business_id", "enabled"],
    )
    op.create_table(
        "authority_policy_versions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("decision_mode", sa.String(40), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("required_scope", sa.String(100), nullable=False),
        sa.Column("approval_role", sa.String(40), nullable=True),
        sa.Column("conditions", sa.JSON(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=True),
        sa.Column("effective_from", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["policy_id"], ["authority_policies.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("policy_id", "version", name="uq_authority_policy_version"),
    )
    op.create_index(
        "ix_authority_policy_versions_business_action",
        "authority_policy_versions", ["business_id", "action_type", "created_at"],
    )


def downgrade() -> None:
    op.drop_table("authority_policy_versions")
    op.drop_table("authority_policies")
    op.drop_table("ai_principal_scopes")
    op.drop_table("ai_service_principals")
    op.drop_table("business_setting_versions")
    op.drop_table("business_settings")
