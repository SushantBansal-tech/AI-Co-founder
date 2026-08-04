"""add durable followup jobs

Revision ID: b8c31a4d9e72
Revises: fa729bd351e8
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8c31a4d9e72"
down_revision: Union[str, Sequence[str], None] = "fa729bd351e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "quotations",
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_unique_constraint(
        "uq_followup_record_quotation_attempt",
        "followup_records",
        ["business_id", "quotation_id", "attempt_number"],
    )
    op.create_table(
        "followup_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("lead_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=False),
        sa.Column("quotation_id", sa.String(36), nullable=False),
        sa.Column("quotation_number", sa.String(30), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("followup_type", sa.String(50), nullable=False),
        sa.Column("tone", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column(
            "scheduled_for",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(30),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "max_attempts",
            sa.Integer(),
            nullable=False,
        ),
        sa.Column(
            "locked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("locked_by", sa.String(100), nullable=True),
        sa.Column(
            "provider_message_id",
            sa.String(255),
            nullable=True,
        ),
        sa.Column(
            "followup_record_id",
            sa.String(36),
            nullable=True,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "cancelled_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "cancellation_reason",
            sa.Text(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(
            ["quotation_id"],
            ["quotations.id"],
        ),
        sa.ForeignKeyConstraint(
            ["followup_record_id"],
            ["followup_records.id"],
        ),
        sa.UniqueConstraint(
            "business_id",
            "quotation_id",
            "attempt_number",
            name="uq_followup_job_quotation_attempt",
        ),
    )
    op.create_index(
        "ix_followup_jobs_due",
        "followup_jobs",
        ["status", "next_attempt_at", "scheduled_for"],
    )
    op.create_index(
        "ix_followup_jobs_tenant_status",
        "followup_jobs",
        ["business_id", "status", "scheduled_for"],
    )
    op.create_index(
        "ix_followup_jobs_thread",
        "followup_jobs",
        ["business_id", "thread_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_followup_jobs_thread",
        table_name="followup_jobs",
    )
    op.drop_index(
        "ix_followup_jobs_tenant_status",
        table_name="followup_jobs",
    )
    op.drop_index(
        "ix_followup_jobs_due",
        table_name="followup_jobs",
    )
    op.drop_table("followup_jobs")
    op.drop_constraint(
        "uq_followup_record_quotation_attempt",
        "followup_records",
        type_="unique",
    )
    op.drop_column("quotations", "sent_at")
