"""add channel conversations and email poll health

Revision ID: a84d2f7c91b6
Revises: d7b4c2e91a63
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a84d2f7c91b6"
down_revision: Union[str, Sequence[str], None] = "d7b4c2e91a63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("channel_sources", sa.Column("last_poll_started_at", sa.DateTime(), nullable=True))
    op.add_column("channel_sources", sa.Column("last_poll_completed_at", sa.DateTime(), nullable=True))
    op.add_column("channel_sources", sa.Column("last_successful_poll_at", sa.DateTime(), nullable=True))
    op.add_column("channel_sources", sa.Column("last_seen_uid", sa.String(255), nullable=True))
    op.add_column(
        "channel_sources",
        sa.Column("last_poll_messages_found", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "channel_sources",
        sa.Column("last_poll_messages_enqueued", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("channel_sources", sa.Column("last_poll_error", sa.Text(), nullable=True))

    op.create_table(
        "channel_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("lead_id", sa.String(36), sa.ForeignKey("leads.id"), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("channel_source_id", sa.String(36), sa.ForeignKey("channel_sources.id"), nullable=False),
        sa.Column("participant_identifier", sa.String(255), nullable=False),
        sa.Column("external_conversation_id", sa.String(255), nullable=True),
        sa.Column("root_message_id", sa.String(255), nullable=True),
        sa.Column("latest_message_id", sa.String(255), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "business_id", "thread_id", "channel",
            name="uq_channel_conversation_thread_channel",
        ),
        sa.UniqueConstraint(
            "business_id", "channel", "external_conversation_id",
            name="uq_channel_conversation_external",
        ),
    )
    op.create_index(
        "ix_channel_conversations_participant_status",
        "channel_conversations",
        ["business_id", "participant_identifier", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_conversations_participant_status",
        table_name="channel_conversations",
    )
    op.drop_table("channel_conversations")
    op.drop_column("channel_sources", "last_poll_error")
    op.drop_column("channel_sources", "last_poll_messages_enqueued")
    op.drop_column("channel_sources", "last_poll_messages_found")
    op.drop_column("channel_sources", "last_seen_uid")
    op.drop_column("channel_sources", "last_successful_poll_at")
    op.drop_column("channel_sources", "last_poll_completed_at")
    op.drop_column("channel_sources", "last_poll_started_at")
