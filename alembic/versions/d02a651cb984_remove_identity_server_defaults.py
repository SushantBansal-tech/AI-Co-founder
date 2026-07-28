"""remove identity server defaults

Revision ID: d02a651cb984
Revises: c71d5f28a904
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d02a651cb984"
down_revision: Union[str, Sequence[str], None] = "c71d5f28a904"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("customers", "status", server_default=None)
    op.alter_column("customer_identities", "is_verified", server_default=None)
    op.alter_column("customer_identities", "is_primary", server_default=None)
    op.alter_column("customer_identities", "source", server_default=None)
    op.alter_column("customer_identities", "created_at", server_default=None)
    op.alter_column("customer_match_reviews", "status", server_default=None)
    op.alter_column(
        "customer_match_reviews", "created_at", server_default=None
    )


def downgrade() -> None:
    op.alter_column("customers", "status", server_default="active")
    op.alter_column(
        "customer_identities", "is_verified", server_default="false"
    )
    op.alter_column(
        "customer_identities", "is_primary", server_default="false"
    )
    op.alter_column(
        "customer_identities", "source", server_default="inquiry"
    )
    op.alter_column(
        "customer_identities", "created_at", server_default="now()"
    )
    op.alter_column(
        "customer_match_reviews", "status", server_default="PENDING"
    )
    op.alter_column(
        "customer_match_reviews", "created_at", server_default="now()"
    )
