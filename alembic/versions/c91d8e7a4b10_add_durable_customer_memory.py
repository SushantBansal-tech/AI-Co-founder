"""add durable customer memory

Revision ID: c91d8e7a4b10
Revises: ef7a91c4d220
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "c91d8e7a4b10"
down_revision: Union[str, Sequence[str], None] = "ef7a91c4d220"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customer_notes",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("interaction_id", sa.String(36), sa.ForeignKey("interactions.id"), nullable=True),
        sa.Column("request_event_id", sa.String(36), nullable=True),
        sa.Column("content_type", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False, server_default="api"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_id", "request_event_id", name="uq_customer_note_request_event"),
    )
    op.create_index("ix_customer_notes_customer_time", "customer_notes", ["business_id", "customer_id", "occurred_at"])
    op.create_index("ix_customer_notes_thread", "customer_notes", ["business_id", "thread_id"])

    op.create_table(
        "memory_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("source_type", sa.String(40), nullable=False),
        sa.Column("source_id", sa.String(100), nullable=False),
        sa.Column("memory_type", sa.String(50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("interaction_id", sa.String(36), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_id", "source_type", "source_id", "memory_type", name="uq_memory_outbox_source"),
    )
    op.create_index("ix_memory_outbox_due", "memory_outbox", ["status", "next_attempt_at"])
    op.create_index("ix_memory_outbox_customer", "memory_outbox", ["business_id", "customer_id", "created_at"])


def downgrade() -> None:
    op.drop_table("memory_outbox")
    op.drop_table("customer_notes")
