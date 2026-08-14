"""add dashboard query indexes

Revision ID: bd9e31f47a20
Revises: a84d2f7c91b6
"""

from typing import Sequence, Union

from alembic import op


revision: str = "bd9e31f47a20"
down_revision: Union[str, Sequence[str], None] = "a84d2f7c91b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_leads_business_created",
        "leads",
        ["business_id", "created_at"],
    )
    op.create_index(
        "ix_quotations_business_sent",
        "quotations",
        ["business_id", "sent_at"],
    )
    op.create_index(
        "ix_sales_orders_business_created",
        "sales_orders",
        ["business_id", "created_at"],
    )
    op.create_index(
        "ix_interactions_business_direction_time",
        "interactions",
        ["business_id", "direction", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_interactions_business_direction_time",
        table_name="interactions",
    )
    op.drop_index(
        "ix_sales_orders_business_created",
        table_name="sales_orders",
    )
    op.drop_index(
        "ix_quotations_business_sent",
        table_name="quotations",
    )
    op.drop_index(
        "ix_leads_business_created",
        table_name="leads",
    )
