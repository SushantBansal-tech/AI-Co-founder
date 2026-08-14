"""add CRM application layer

Revision ID: e51c9a7d2f40
Revises: bd9e31f47a20
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e51c9a7d2f40"
down_revision: Union[str, Sequence[str], None] = "bd9e31f47a20"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("normalized_email", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("normalized_email", name="uq_users_normalized_email"),
    )
    op.create_index("ix_users_status", "users", ["status"])

    op.create_table(
        "business_memberships",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(40), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_id", "user_id", name="uq_business_membership_user"),
    )
    op.create_index("ix_business_memberships_user_id", "business_memberships", ["user_id"])
    op.create_index(
        "ix_business_memberships_business_role",
        "business_memberships",
        ["business_id", "role", "status"],
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_auth_sessions_token_hash"),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expiry", "auth_sessions", ["expires_at", "revoked_at"])

    op.add_column("customers", sa.Column("account_owner_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_customers_account_owner_id_users",
        "customers", "users", ["account_owner_id"], ["id"],
    )
    op.create_index("ix_customers_account_owner_id", "customers", ["account_owner_id"])

    op.add_column("pipeline_instances", sa.Column("approval_stage", sa.String(60), nullable=True))

    op.add_column("leads", sa.Column("assigned_to_user_id", sa.String(36), nullable=True))
    op.add_column("leads", sa.Column("assigned_at", sa.DateTime(), nullable=True))
    op.add_column("leads", sa.Column("assigned_by_user_id", sa.String(36), nullable=True))
    op.add_column("leads", sa.Column("lost_reason_code", sa.String(50), nullable=True))
    op.add_column("leads", sa.Column("lost_reason_notes", sa.Text(), nullable=True))
    op.add_column("leads", sa.Column("competitor_name", sa.String(255), nullable=True))
    op.add_column("leads", sa.Column("lost_value", sa.Numeric(18, 2), nullable=True))
    op.add_column("leads", sa.Column("closed_lost_at", sa.DateTime(), nullable=True))
    op.add_column("leads", sa.Column("closed_lost_by_user_id", sa.String(36), nullable=True))
    op.create_foreign_key(
        "fk_leads_assigned_to_user_id_users",
        "leads", "users", ["assigned_to_user_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_leads_assigned_by_user_id_users",
        "leads", "users", ["assigned_by_user_id"], ["id"],
    )
    op.create_foreign_key(
        "fk_leads_closed_lost_by_user_id_users",
        "leads", "users", ["closed_lost_by_user_id"], ["id"],
    )
    op.create_index("ix_leads_assigned_to_user_id", "leads", ["assigned_to_user_id"])
    op.create_index("ix_leads_lost_reason_code", "leads", ["lost_reason_code"])

    op.create_table(
        "lead_assignments",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("lead_id", sa.String(36), nullable=False),
        sa.Column("assigned_to_user_id", sa.String(36), nullable=False),
        sa.Column("assigned_by_user_id", sa.String(36), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["assigned_to_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_lead_assignments_current", "lead_assignments",
        ["business_id", "lead_id", "ended_at"],
    )
    op.create_index(
        "ix_lead_assignments_user", "lead_assignments",
        ["business_id", "assigned_to_user_id", "ended_at"],
    )

    op.create_table(
        "crm_tasks",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("lead_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("assigned_to_user_id", sa.String(36), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("task_type", sa.String(50), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("priority", sa.String(20), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("due_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("completion_notes", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["assigned_to_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_crm_tasks_assignee_due", "crm_tasks",
        ["business_id", "assigned_to_user_id", "status", "due_at"],
    )
    op.create_index("ix_crm_tasks_customer", "crm_tasks", ["business_id", "customer_id", "created_at"])
    op.create_index("ix_crm_tasks_lead", "crm_tasks", ["business_id", "lead_id", "created_at"])

    op.create_table(
        "crm_activities",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("lead_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("activity_type", sa.String(60), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("outcome", sa.String(100), nullable=True),
        sa.Column("actor_user_id", sa.String(36), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["task_id"], ["crm_tasks.id"]),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_crm_activities_customer", "crm_activities", ["business_id", "customer_id", "occurred_at"])
    op.create_index("ix_crm_activities_lead", "crm_activities", ["business_id", "lead_id", "occurred_at"])


def downgrade() -> None:
    op.drop_table("crm_activities")
    op.drop_table("crm_tasks")
    op.drop_table("lead_assignments")

    op.drop_index("ix_leads_lost_reason_code", table_name="leads")
    op.drop_index("ix_leads_assigned_to_user_id", table_name="leads")
    op.drop_constraint("fk_leads_closed_lost_by_user_id_users", "leads", type_="foreignkey")
    op.drop_constraint("fk_leads_assigned_by_user_id_users", "leads", type_="foreignkey")
    op.drop_constraint("fk_leads_assigned_to_user_id_users", "leads", type_="foreignkey")
    for column in (
        "closed_lost_by_user_id", "closed_lost_at", "lost_value",
        "competitor_name", "lost_reason_notes", "lost_reason_code",
        "assigned_by_user_id", "assigned_at", "assigned_to_user_id",
    ):
        op.drop_column("leads", column)

    op.drop_index("ix_customers_account_owner_id", table_name="customers")
    op.drop_constraint("fk_customers_account_owner_id_users", "customers", type_="foreignkey")
    op.drop_column("customers", "account_owner_id")
    op.drop_column("pipeline_instances", "approval_stage")

    op.drop_table("auth_sessions")
    op.drop_table("business_memberships")
    op.drop_table("users")
