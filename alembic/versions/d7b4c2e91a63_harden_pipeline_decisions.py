"""harden pipeline decisions

Revision ID: d7b4c2e91a63
Revises: c91d8e7a4b10
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "d7b4c2e91a63"
down_revision: Union[str, Sequence[str], None] = "c91d8e7a4b10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pipeline_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("thread_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("lead_id", sa.String(36), sa.ForeignKey("leads.id"), nullable=True),
        sa.Column("pipeline_status", sa.String(60), nullable=False, server_default="processing"),
        sa.Column("business_milestone", sa.String(60), nullable=True),
        sa.Column("waiting_for", sa.String(60), nullable=False, server_default="none"),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("current_node", sa.String(100), nullable=True),
        sa.Column("failure_category", sa.String(60), nullable=True),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("failure_details", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_id", "thread_id", name="uq_pipeline_instance_thread"),
    )
    op.create_index("ix_pipeline_instances_status", "pipeline_instances", ["business_id", "pipeline_status", "updated_at"])
    op.create_index("ix_pipeline_instances_waiting", "pipeline_instances", ["business_id", "waiting_for", "updated_at"])

    op.create_table(
        "quotation_delivery_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("quotation_id", sa.String(36), sa.ForeignKey("quotations.id"), nullable=False),
        sa.Column("thread_id", sa.String(100), nullable=False),
        sa.Column("channel", sa.String(30), nullable=False),
        sa.Column("recipient", sa.String(255), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="prepared"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("provider_message_id", sa.String(255), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_id", "quotation_id", "channel", "recipient", name="uq_quotation_delivery_target"),
    )
    op.create_index("ix_quotation_delivery_status", "quotation_delivery_attempts", ["business_id", "status", "created_at"])

    op.create_table(
        "inventory_reservations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=True),
        sa.Column("po_id", sa.String(36), sa.ForeignKey("purchase_orders.id"), nullable=False),
        sa.Column("inventory_record_id", sa.String(36), sa.ForeignKey("inventory_records.id"), nullable=False),
        sa.Column("product_code", sa.String(80), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="reserved"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("business_id", "po_id", "inventory_record_id", name="uq_inventory_reservation_po_row"),
    )
    op.create_index("ix_inventory_reservations_product", "inventory_reservations", ["business_id", "product_code", "status"])


def downgrade() -> None:
    op.drop_table("inventory_reservations")
    op.drop_table("quotation_delivery_attempts")
    op.drop_table("pipeline_instances")
