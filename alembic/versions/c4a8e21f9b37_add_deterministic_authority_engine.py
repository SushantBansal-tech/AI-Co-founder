"""add deterministic authority decisions and durable approvals

Revision ID: c4a8e21f9b37
Revises: b24f7a91c3d8
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4a8e21f9b37"
down_revision: Union[str, Sequence[str], None] = "b24f7a91c3d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "authority_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", sa.String(100), nullable=True),
        sa.Column("tool_execution_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("decision", sa.String(40), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("policy_code", sa.String(100), nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("settings_version", sa.Integer(), nullable=True),
        sa.Column("approval_role", sa.String(40), nullable=True),
        sa.Column("facts_snapshot", sa.JSON(), nullable=False),
        sa.Column("reasons", sa.JSON(), nullable=False),
        sa.Column("missing_information", sa.JSON(), nullable=False),
        sa.Column("missing_master_data", sa.JSON(), nullable=False),
        sa.Column("evidence_chunk_ids", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["ai_service_principals.id"]),
        sa.ForeignKeyConstraint(["tool_execution_id"], ["ai_tool_executions.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["authority_policies.id"]),
    )
    op.create_index("ix_authority_decisions_business_time", "authority_decisions", ["business_id", "created_at"])
    op.create_index("ix_authority_decisions_business_action", "authority_decisions", ["business_id", "action_type", "decision"])
    op.create_index("ix_authority_decisions_business_thread", "authority_decisions", ["business_id", "thread_id"])
    op.create_index("ix_authority_decisions_business_entity", "authority_decisions", ["business_id", "entity_type", "entity_id"])

    op.create_table(
        "authority_approval_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("authority_decision_id", sa.String(36), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", sa.String(100), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("requested_by_principal_id", sa.String(36), nullable=False),
        sa.Column("required_role", sa.String(40), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("settings_version", sa.Integer(), nullable=True),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("approved_by_user_id", sa.String(36), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("rejected_by_user_id", sa.String(36), nullable=True),
        sa.Column("rejected_at", sa.DateTime(), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("consumed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["authority_decision_id"], ["authority_decisions.id"]),
        sa.ForeignKeyConstraint(["requested_by_principal_id"], ["ai_service_principals.id"]),
        sa.ForeignKeyConstraint(["policy_id"], ["authority_policies.id"]),
        sa.ForeignKeyConstraint(["approved_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["rejected_by_user_id"], ["users.id"]),
        sa.UniqueConstraint("authority_decision_id", name="uq_authority_approval_decision"),
    )
    op.create_index("ix_authority_approvals_business_status", "authority_approval_requests", ["business_id", "status", "created_at"])
    op.create_index("ix_authority_approvals_business_thread", "authority_approval_requests", ["business_id", "thread_id"])

    op.execute(sa.text("""
        WITH specs(action_type, name, decision_mode, risk_level, required_scope, approval_role, conditions) AS (
            VALUES
              ('message_send','Send customer message','threshold_auto','medium','message:send','sales_manager',json_build_object('respect_consent_and_daily_limit', true)),
              ('quotation_send','Send quotation','threshold_auto','high','quotation:send','finance_manager',json_build_object('requires_complete_pricing', true)),
              ('po_validate','Validate purchase order','auto_execute','medium','order:prepare','production_manager',json_build_object('deterministic_validation_only', true)),
              ('po_accept','Accept purchase order','threshold_auto','high','order:accept','production_manager',json_build_object('requires_revalidation', true)),
              ('sales_order_create','Create sales order','threshold_auto','high','order:accept','production_manager',json_build_object('requires_valid_po_and_fulfillment_allocation', true)),
              ('deal_close_won','Close deal as won','threshold_auto','high','deal:close','sales_manager',json_build_object('requires_valid_order_acceptance', true)),
              ('deal_close_lost','Close deal as lost','approval_required','medium','deal:close','sales_manager',json_build_object('requires_reason', true))
        ), inserted AS (
            INSERT INTO authority_policies
                (id, business_id, action_type, name, description, active_version, enabled, created_at, updated_at)
            SELECT md5(bs.business_id || '-batch3-' || s.action_type), bs.business_id,
                   s.action_type, s.name, 'Default deterministic Batch 3 policy',
                   1, true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM business_settings bs CROSS JOIN specs s
            ON CONFLICT (business_id, action_type) DO NOTHING
            RETURNING id, business_id, action_type
        )
        INSERT INTO authority_policy_versions
            (id, business_id, policy_id, action_type, version, decision_mode,
             risk_level, required_scope, approval_role, conditions, change_reason,
             created_by_user_id, effective_from, created_at)
        SELECT md5(p.business_id || '-batch3-' || p.action_type || '-v1'),
               p.business_id, p.id, p.action_type, 1, s.decision_mode,
               s.risk_level, s.required_scope, s.approval_role, s.conditions,
               'Safe Batch 3 default policy', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM inserted p JOIN specs s ON s.action_type = p.action_type
        ON CONFLICT (policy_id, version) DO NOTHING
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        DELETE FROM authority_policy_versions WHERE action_type IN
          ('message_send','quotation_send','po_validate','po_accept','sales_order_create','deal_close_won','deal_close_lost');
        DELETE FROM authority_policies WHERE action_type IN
          ('message_send','quotation_send','po_validate','po_accept','sales_order_create','deal_close_won','deal_close_lost');
    """))
    op.drop_table("authority_approval_requests")
    op.drop_table("authority_decisions")
