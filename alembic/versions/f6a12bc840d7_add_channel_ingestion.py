"""add shared channel ingestion

Revision ID: f6a12bc840d7
Revises: e47c8a61f203
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6a12bc840d7"
down_revision: Union[str, Sequence[str], None] = "e47c8a61f203"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "channel_sources",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("public_key", sa.String(100), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("configuration", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "public_key",
            name="uq_channel_sources_public_key",
        ),
    )
    op.create_index(
        "ix_channel_sources_business_id",
        "channel_sources",
        ["business_id"],
    )
    op.create_index(
        "ix_channel_sources_public_key",
        "channel_sources",
        ["public_key"],
    )
    op.create_index(
        "ix_channel_sources_business_channel_active",
        "channel_sources",
        ["business_id", "channel", "active"],
    )

    op.create_table(
        "channel_ingestions",
        sa.Column("id", sa.String(36), nullable=False),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("channel_source_id", sa.String(36), nullable=False),
        sa.Column("interaction_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("normalized_payload", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_source_id"], ["channel_sources.id"]
        ),
        sa.ForeignKeyConstraint(["interaction_id"], ["interactions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id",
            "channel",
            "provider",
            "external_event_id",
            name="uq_channel_ingestion_external_event",
        ),
    )
    op.create_index(
        "ix_channel_ingestions_business_status_received",
        "channel_ingestions",
        ["business_id", "status", "received_at"],
    )
    op.create_index(
        "ix_channel_ingestions_thread",
        "channel_ingestions",
        ["business_id", "thread_id"],
    )
    op.create_index(
        "ix_channel_ingestions_source_received",
        "channel_ingestions",
        ["channel_source_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_ingestions_source_received",
        table_name="channel_ingestions",
    )
    op.drop_index(
        "ix_channel_ingestions_thread",
        table_name="channel_ingestions",
    )
    op.drop_index(
        "ix_channel_ingestions_business_status_received",
        table_name="channel_ingestions",
    )
    op.drop_table("channel_ingestions")

    op.drop_index(
        "ix_channel_sources_business_channel_active",
        table_name="channel_sources",
    )
    op.drop_index(
        "ix_channel_sources_public_key",
        table_name="channel_sources",
    )
    op.drop_index(
        "ix_channel_sources_business_id",
        table_name="channel_sources",
    )
    op.drop_table("channel_sources")
