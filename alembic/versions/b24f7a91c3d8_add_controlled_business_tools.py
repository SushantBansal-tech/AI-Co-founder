"""add controlled Jarvis business tools

Revision ID: b24f7a91c3d8
Revises: a19e4d6b2c71
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b24f7a91c3d8"
down_revision: Union[str, Sequence[str], None] = "a19e4d6b2c71"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("crm_tasks", sa.Column("created_by_principal_id", sa.String(36), nullable=True))
    op.alter_column("crm_tasks", "created_by_user_id", existing_type=sa.String(36), nullable=True)
    op.create_foreign_key(
        "fk_crm_tasks_created_by_principal", "crm_tasks", "ai_service_principals",
        ["created_by_principal_id"], ["id"],
    )
    op.create_check_constraint(
        "ck_crm_tasks_creator", "crm_tasks",
        "created_by_user_id IS NOT NULL OR created_by_principal_id IS NOT NULL",
    )

    op.add_column("crm_activities", sa.Column("actor_principal_id", sa.String(36), nullable=True))
    op.alter_column("crm_activities", "actor_user_id", existing_type=sa.String(36), nullable=True)
    op.create_foreign_key(
        "fk_crm_activities_actor_principal", "crm_activities", "ai_service_principals",
        ["actor_principal_id"], ["id"],
    )
    op.create_check_constraint(
        "ck_crm_activities_actor", "crm_activities",
        "actor_user_id IS NOT NULL OR actor_principal_id IS NOT NULL",
    )

    op.add_column("customer_notes", sa.Column("created_by_principal_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_customer_notes_creator_principal", "customer_notes", "ai_service_principals",
        ["created_by_principal_id"], ["id"],
    )
    op.add_column("followup_jobs", sa.Column("created_by_principal_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_followup_jobs_creator_principal", "followup_jobs", "ai_service_principals",
        ["created_by_principal_id"], ["id"],
    )
    op.add_column("quotations", sa.Column("prepared_by_principal_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_quotations_prepared_principal", "quotations", "ai_service_principals",
        ["prepared_by_principal_id"], ["id"],
    )
    op.add_column("quotation_versions", sa.Column("changed_by_principal_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_quotation_versions_changed_principal", "quotation_versions", "ai_service_principals",
        ["changed_by_principal_id"], ["id"],
    )

    op.create_table(
        "ai_tool_executions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("required_scope", sa.String(100), nullable=False),
        sa.Column("is_mutation", sa.Boolean(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("authority_decision", sa.String(40), nullable=True),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("response_json", sa.JSON(), nullable=True),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", sa.String(100), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["principal_id"], ["ai_service_principals.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id", "principal_id", "tool_name", "idempotency_key",
            name="uq_ai_tool_execution_idempotency",
        ),
    )
    op.create_index(
        "ix_ai_tool_executions_business_time", "ai_tool_executions",
        ["business_id", "started_at"],
    )
    op.create_index(
        "ix_ai_tool_executions_principal_status", "ai_tool_executions",
        ["business_id", "principal_id", "status"],
    )

    # Existing tenants already initialized in Batch 1 need the five new policy
    # rows. IDs are deterministic strings; no PostgreSQL extension is required.
    op.execute(sa.text("""
        WITH specs(action_type, name, decision_mode, risk_level, required_scope, approval_role, conditions) AS (
            VALUES
              ('add_customer_note','Add customer note','auto_execute','low','customer_note:create',NULL,'{}'::json),
              ('create_task','Create CRM task','auto_execute','low','task:create',NULL,'{}'::json),
              ('record_activity','Record CRM activity','auto_execute','low','activity:create',NULL,'{}'::json),
              ('schedule_followup','Schedule quotation follow-up','auto_execute','low','followup:schedule',NULL,'{}'::json),
              ('prepare_quotation','Prepare quotation draft','prepare_only','medium','quotation:prepare','finance_manager',json_build_object('dispatch_prohibited', true))
        ), inserted AS (
            INSERT INTO authority_policies
                (id, business_id, action_type, name, description, active_version, enabled, created_at, updated_at)
            SELECT md5(bs.business_id || '-' || s.action_type), bs.business_id, s.action_type,
                   s.name, 'Default controlled business tool policy', 1, true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM business_settings bs CROSS JOIN specs s
            ON CONFLICT (business_id, action_type) DO NOTHING
            RETURNING id, business_id, action_type
        )
        INSERT INTO authority_policy_versions
            (id, business_id, policy_id, action_type, version, decision_mode, risk_level,
             required_scope, approval_role, conditions, change_reason,
             created_by_user_id, effective_from, created_at)
        SELECT md5(p.business_id || '-' || p.action_type || '-v1'), p.business_id, p.id,
               p.action_type, 1, s.decision_mode, s.risk_level, s.required_scope,
               s.approval_role, s.conditions, 'Safe Batch 2 default policy',
               NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        FROM inserted p JOIN specs s ON s.action_type = p.action_type
        ON CONFLICT (policy_id, version) DO NOTHING
    """))


def downgrade() -> None:
    op.execute(sa.text("""
        DELETE FROM authority_policy_versions
        WHERE action_type IN ('add_customer_note','create_task','record_activity','schedule_followup','prepare_quotation');
        DELETE FROM authority_policies
        WHERE action_type IN ('add_customer_note','create_task','record_activity','schedule_followup','prepare_quotation');
    """))
    op.drop_table("ai_tool_executions")
    op.drop_constraint("fk_quotation_versions_changed_principal", "quotation_versions", type_="foreignkey")
    op.drop_column("quotation_versions", "changed_by_principal_id")
    op.drop_constraint("fk_quotations_prepared_principal", "quotations", type_="foreignkey")
    op.drop_column("quotations", "prepared_by_principal_id")
    op.drop_constraint("fk_followup_jobs_creator_principal", "followup_jobs", type_="foreignkey")
    op.drop_column("followup_jobs", "created_by_principal_id")
    op.drop_constraint("fk_customer_notes_creator_principal", "customer_notes", type_="foreignkey")
    op.drop_column("customer_notes", "created_by_principal_id")
    op.drop_constraint("ck_crm_activities_actor", "crm_activities", type_="check")
    op.drop_constraint("fk_crm_activities_actor_principal", "crm_activities", type_="foreignkey")
    op.alter_column("crm_activities", "actor_user_id", existing_type=sa.String(36), nullable=False)
    op.drop_column("crm_activities", "actor_principal_id")
    op.drop_constraint("ck_crm_tasks_creator", "crm_tasks", type_="check")
    op.drop_constraint("fk_crm_tasks_created_by_principal", "crm_tasks", type_="foreignkey")
    op.alter_column("crm_tasks", "created_by_user_id", existing_type=sa.String(36), nullable=False)
    op.drop_column("crm_tasks", "created_by_principal_id")
