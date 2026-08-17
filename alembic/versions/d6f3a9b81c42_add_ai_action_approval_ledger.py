"""add durable Jarvis action and approval ledger

Revision ID: d6f3a9b81c42
Revises: c4a8e21f9b37
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d6f3a9b81c42"
down_revision: Union[str, Sequence[str], None] = "c4a8e21f9b37"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_action_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.Column("tool_name", sa.String(100), nullable=False),
        sa.Column("action_type", sa.String(80), nullable=False),
        sa.Column("risk_level", sa.String(20), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("arguments_json", sa.JSON(), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("lead_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", sa.String(100), nullable=True),
        sa.Column("latest_authority_decision_id", sa.String(36), nullable=True),
        sa.Column("active_approval_request_id", sa.String(36), nullable=True),
        sa.Column("latest_tool_execution_id", sa.String(36), nullable=True),
        sa.Column("policy_code", sa.String(100), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("settings_version", sa.Integer(), nullable=True),
        sa.Column("approval_role", sa.String(40), nullable=True),
        sa.Column("evaluated_facts_json", sa.JSON(), nullable=True),
        sa.Column("execution_result_json", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("execution_attempt_count", sa.Integer(), nullable=False),
        sa.Column("proposed_at", sa.DateTime(), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("execution_started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["principal_id"], ["ai_service_principals.id"]),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["latest_authority_decision_id"], ["authority_decisions.id"]),
        sa.ForeignKeyConstraint(["active_approval_request_id"], ["authority_approval_requests.id"]),
        sa.ForeignKeyConstraint(["latest_tool_execution_id"], ["ai_tool_executions.id"]),
        sa.UniqueConstraint(
            "business_id", "principal_id", "tool_name", "idempotency_key",
            name="uq_ai_action_request_idempotency",
        ),
    )
    op.create_index(
        "ix_ai_actions_business_status", "ai_action_requests",
        ["business_id", "status", "proposed_at"],
    )
    op.create_index(
        "ix_ai_actions_business_customer", "ai_action_requests",
        ["business_id", "customer_id", "proposed_at"],
    )
    op.create_index(
        "ix_ai_actions_business_thread", "ai_action_requests",
        ["business_id", "thread_id", "proposed_at"],
    )

    op.add_column("authority_decisions", sa.Column("action_request_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_authority_decisions_action_request", "authority_decisions",
        "ai_action_requests", ["action_request_id"], ["id"],
    )
    op.add_column("authority_approval_requests", sa.Column("action_request_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_authority_approvals_action_request", "authority_approval_requests",
        "ai_action_requests", ["action_request_id"], ["id"],
    )
    op.add_column("ai_tool_executions", sa.Column("action_request_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_ai_tool_executions_action_request", "ai_tool_executions",
        "ai_action_requests", ["action_request_id"], ["id"],
    )

    op.create_table(
        "approval_decisions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("action_request_id", sa.String(36), nullable=True),
        sa.Column("approval_request_id", sa.String(36), nullable=False),
        sa.Column("authority_decision_id", sa.String(36), nullable=False),
        sa.Column("decision", sa.String(20), nullable=False),
        sa.Column("decided_by_user_id", sa.String(36), nullable=False),
        sa.Column("decided_by_role", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=True),
        sa.Column("policy_version", sa.Integer(), nullable=True),
        sa.Column("settings_version", sa.Integer(), nullable=True),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["action_request_id"], ["ai_action_requests.id"]),
        sa.ForeignKeyConstraint(["approval_request_id"], ["authority_approval_requests.id"]),
        sa.ForeignKeyConstraint(["authority_decision_id"], ["authority_decisions.id"]),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"]),
        sa.UniqueConstraint("approval_request_id", name="uq_approval_decision_request"),
    )
    op.create_index(
        "ix_approval_decisions_business_time", "approval_decisions",
        ["business_id", "decided_at"],
    )
    op.create_index(
        "ix_approval_decisions_action", "approval_decisions",
        ["action_request_id", "decided_at"],
    )


def downgrade() -> None:
    op.drop_table("approval_decisions")
    op.drop_constraint("fk_ai_tool_executions_action_request", "ai_tool_executions", type_="foreignkey")
    op.drop_column("ai_tool_executions", "action_request_id")
    op.drop_constraint("fk_authority_approvals_action_request", "authority_approval_requests", type_="foreignkey")
    op.drop_column("authority_approval_requests", "action_request_id")
    op.drop_constraint("fk_authority_decisions_action_request", "authority_decisions", type_="foreignkey")
    op.drop_column("authority_decisions", "action_request_id")
    op.drop_table("ai_action_requests")
