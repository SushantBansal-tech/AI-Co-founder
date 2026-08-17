"""add durable Jarvis conversations and orchestrator runs

Revision ID: e7b4c19a2d53
Revises: d6f3a9b81c42
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "e7b4c19a2d53"
down_revision: Union[str, Sequence[str], None] = "d6f3a9b81c42"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jarvis_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("created_by_user_id", sa.String(36), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["users.id"]),
    )
    op.create_index(
        "ix_jarvis_conversations_business_user_time", "jarvis_conversations",
        ["business_id", "created_by_user_id", "updated_at"],
    )
    op.create_index(
        "ix_jarvis_conversations_business_status", "jarvis_conversations",
        ["business_id", "status"],
    )

    op.create_table(
        "jarvis_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("tool_call_id", sa.String(100), nullable=True),
        sa.Column("action_request_id", sa.String(36), nullable=True),
        sa.Column("supporting_data", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["jarvis_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["action_request_id"], ["ai_action_requests.id"]),
    )
    op.create_index(
        "ix_jarvis_messages_business_conversation_time", "jarvis_messages",
        ["business_id", "conversation_id", "created_at"],
    )
    op.create_index(
        "ix_jarvis_messages_action", "jarvis_messages", ["action_request_id"]
    )

    op.create_table(
        "jarvis_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("conversation_id", sa.String(36), nullable=False),
        sa.Column("requested_by_user_id", sa.String(36), nullable=False),
        sa.Column("principal_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("input_message_id", sa.String(36), nullable=False),
        sa.Column("final_message_id", sa.String(36), nullable=True),
        sa.Column("plan_json", sa.JSON(), nullable=True),
        sa.Column("tool_results_json", sa.JSON(), nullable=False),
        sa.Column("supporting_data", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"], ["jarvis_conversations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["principal_id"], ["ai_service_principals.id"]),
        sa.ForeignKeyConstraint(["input_message_id"], ["jarvis_messages.id"]),
        sa.ForeignKeyConstraint(["final_message_id"], ["jarvis_messages.id"]),
    )
    op.create_index(
        "ix_jarvis_runs_business_status_time", "jarvis_runs",
        ["business_id", "status", "started_at"],
    )
    op.create_index(
        "ix_jarvis_runs_conversation_time", "jarvis_runs",
        ["conversation_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_table("jarvis_runs")
    op.drop_table("jarvis_messages")
    op.drop_table("jarvis_conversations")
