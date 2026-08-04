"""add email and whatsapp channel adapters

Revision ID: fa729bd351e8
Revises: f6a12bc840d7
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "fa729bd351e8"
down_revision: Union[str, Sequence[str], None] = "f6a12bc840d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channel_sources",
        sa.Column("provider_account_id", sa.String(255), nullable=True),
    )
    op.create_index(
        "ix_channel_sources_provider_account_id",
        "channel_sources",
        ["provider_account_id"],
    )
    op.create_unique_constraint(
        "uq_channel_source_provider_account",
        "channel_sources",
        ["channel", "provider", "provider_account_id"],
    )

    op.create_table(
        "channel_cursors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("channel_source_id", sa.String(36), nullable=False),
        sa.Column("cursor_type", sa.String(50), nullable=False),
        sa.Column("cursor_value", sa.String(255), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["channel_source_id"], ["channel_sources.id"]),
        sa.UniqueConstraint(
            "channel_source_id",
            "cursor_type",
            name="uq_channel_cursor_source_type",
        ),
    )
    op.create_index(
        "ix_channel_cursors_business_source",
        "channel_cursors",
        ["business_id", "channel_source_id"],
    )

    op.create_table(
        "channel_inbound_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("channel_source_id", sa.String(36), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("external_event_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("normalized_payload", sa.JSON(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("locked_by", sa.String(100), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
        sa.Column("ingestion_id", sa.String(36), nullable=True),
        sa.Column("thread_id", sa.String(100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["channel_source_id"], ["channel_sources.id"]),
        sa.ForeignKeyConstraint(["ingestion_id"], ["channel_ingestions.id"]),
        sa.UniqueConstraint(
            "business_id",
            "channel",
            "provider",
            "external_event_id",
            name="uq_channel_inbound_job_external_event",
        ),
    )
    op.create_index(
        "ix_channel_inbound_jobs_due",
        "channel_inbound_jobs",
        ["status", "next_attempt_at", "created_at"],
    )
    op.create_index(
        "ix_channel_inbound_jobs_tenant_status",
        "channel_inbound_jobs",
        ["business_id", "status", "created_at"],
    )

    op.create_table(
        "channel_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("inbound_job_id", sa.String(36), nullable=False),
        sa.Column("provider_file_id", sa.String(255), nullable=True),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("storage_path", sa.String(1000), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("scan_status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["inbound_job_id"], ["channel_inbound_jobs.id"]),
        sa.UniqueConstraint(
            "inbound_job_id",
            "checksum_sha256",
            name="uq_channel_attachment_job_checksum",
        ),
    )
    op.create_index(
        "ix_channel_attachments_business_job",
        "channel_attachments",
        ["business_id", "inbound_job_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_channel_attachments_business_job",
        table_name="channel_attachments",
    )
    op.drop_table("channel_attachments")
    op.drop_index(
        "ix_channel_inbound_jobs_tenant_status",
        table_name="channel_inbound_jobs",
    )
    op.drop_index(
        "ix_channel_inbound_jobs_due",
        table_name="channel_inbound_jobs",
    )
    op.drop_table("channel_inbound_jobs")
    op.drop_index(
        "ix_channel_cursors_business_source",
        table_name="channel_cursors",
    )
    op.drop_table("channel_cursors")
    op.drop_constraint(
        "uq_channel_source_provider_account",
        "channel_sources",
        type_="unique",
    )
    op.drop_index(
        "ix_channel_sources_provider_account_id",
        table_name="channel_sources",
    )
    op.drop_column("channel_sources", "provider_account_id")
