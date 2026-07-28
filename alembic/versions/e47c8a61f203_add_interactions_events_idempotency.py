"""add interactions events and idempotency

Revision ID: e47c8a61f203
Revises: d02a651cb984
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e47c8a61f203"
down_revision: Union[str, Sequence[str], None] = "d02a651cb984"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "interactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("lead_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("direction", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("message_type", sa.String(50), nullable=False),
        sa.Column("external_message_id", sa.String(255), nullable=True),
        sa.Column("sender", sa.String(255), nullable=True),
        sa.Column("recipient", sa.String(255), nullable=True),
        sa.Column("subject", sa.String(500), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.UniqueConstraint(
            "business_id", "channel", "external_message_id",
            name="uq_interaction_external_message",
        ),
    )
    op.create_index(
        "ix_interactions_customer_occurred", "interactions",
        ["business_id", "customer_id", "occurred_at"],
    )
    op.create_index(
        "ix_interactions_thread_occurred", "interactions",
        ["business_id", "thread_id", "occurred_at"],
    )

    op.create_table(
        "business_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("lead_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("source", sa.String(30), nullable=False),
        sa.Column("actor_type", sa.String(30), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=True),
        sa.Column("entity_type", sa.String(50), nullable=True),
        sa.Column("entity_id", sa.String(100), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
    )
    op.create_index(
        "ix_business_events_customer_time", "business_events",
        ["business_id", "customer_id", "occurred_at"],
    )
    op.create_index(
        "ix_business_events_thread_time", "business_events",
        ["business_id", "thread_id", "occurred_at"],
    )
    op.create_index(
        "ix_business_events_type_time", "business_events",
        ["business_id", "event_type", "occurred_at"],
    )

    op.create_table(
        "processed_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("endpoint", sa.String(150), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("interaction_id", sa.String(36), nullable=True),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "business_id", "endpoint", "idempotency_key",
            name="uq_processed_event_idempotency",
        ),
    )
    op.create_index(
        "ix_processed_events_status_created", "processed_events",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_processed_events_status_created", table_name="processed_events"
    )
    op.drop_table("processed_events")
    for name in (
        "ix_business_events_type_time",
        "ix_business_events_thread_time",
        "ix_business_events_customer_time",
    ):
        op.drop_index(name, table_name="business_events")
    op.drop_table("business_events")
    op.drop_index(
        "ix_interactions_thread_occurred", table_name="interactions"
    )
    op.drop_index(
        "ix_interactions_customer_occurred", table_name="interactions"
    )
    op.drop_table("interactions")
