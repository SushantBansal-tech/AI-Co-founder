"""add structured business data

Revision ID: ef7a91c4d220
Revises: da3ff1ab50f4
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "ef7a91c4d220"
down_revision: Union[str, Sequence[str], None] = "da3ff1ab50f4"
branch_labels = None
depends_on = None


def common():
    return [
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("source_document_id", sa.String(36), sa.ForeignKey("business_documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    ]


def upgrade() -> None:
    op.create_table(
        "business_documents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("business_id", sa.String(100), nullable=False),
        sa.Column("logical_name", sa.String(255), nullable=False),
        sa.Column("original_filename", sa.String(255), nullable=False),
        sa.Column("document_type", sa.String(80), nullable=False),
        sa.Column("version", sa.String(50), nullable=False),
        sa.Column("checksum_sha256", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("import_status", sa.String(30), nullable=False, server_default="processing"),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("validation_errors", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime()),
        sa.UniqueConstraint("business_id", "logical_name", "version", name="uq_business_document_version"),
    )
    op.create_table("catalog_products", *common(),
        sa.Column("product_code", sa.String(80), nullable=False), sa.Column("name", sa.String(255), nullable=False),
        sa.Column("category", sa.String(120), nullable=False), sa.Column("grade", sa.String(120)),
        sa.Column("specifications", sa.Text()), sa.Column("unit", sa.String(30), nullable=False, server_default="MT"),
        sa.UniqueConstraint("source_document_id", "product_code", name="uq_catalog_document_product"))
    op.create_table("inventory_records", *common(),
        sa.Column("product_code", sa.String(80), nullable=False), sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("warehouse", sa.String(255), nullable=False), sa.Column("physical_qty", sa.Numeric(18,3), server_default="0"),
        sa.Column("reserved_qty", sa.Numeric(18,3), server_default="0"), sa.Column("available_qty", sa.Numeric(18,3), server_default="0"),
        sa.Column("damaged_qty", sa.Numeric(18,3), server_default="0"), sa.Column("reorder_level", sa.Numeric(18,3), server_default="0"),
        sa.Column("stock_status", sa.String(50)), sa.Column("last_updated", sa.DateTime()))
    op.create_table("production_capacity_records", *common(),
        sa.Column("product_code", sa.String(80), nullable=False), sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("plant", sa.String(255), nullable=False), sa.Column("daily_capacity", sa.Numeric(18,3), server_default="0"),
        sa.Column("current_daily_load", sa.Numeric(18,3), server_default="0"), sa.Column("available_daily_capacity", sa.Numeric(18,3), server_default="0"),
        sa.Column("active_shifts", sa.Integer(), server_default="0"), sa.Column("estimated_lead_time_days", sa.Integer(), server_default="0"),
        sa.Column("capacity_status", sa.String(50)), sa.Column("earliest_completion_date", sa.Date()))
    op.create_table("delivery_zone_records", *common(),
        sa.Column("zone_code", sa.String(80), nullable=False), sa.Column("city", sa.String(120), nullable=False),
        sa.Column("state", sa.String(120)), sa.Column("region", sa.String(120)), sa.Column("pincode_start", sa.String(12)),
        sa.Column("pincode_end", sa.String(12)), sa.Column("transit_days", sa.Integer(), nullable=False),
        sa.Column("preferred_mode", sa.String(80)), sa.Column("minimum_freight_inr", sa.Numeric(18,2), server_default="0"),
        sa.Column("service_level", sa.String(80)), sa.Column("status", sa.String(30), nullable=False, server_default="active"))
    op.create_table("product_price_records", *common(),
        sa.Column("product_code", sa.String(80), nullable=False), sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("unit", sa.String(30), server_default="MT"), sa.Column("base_price_inr", sa.Numeric(18,2), nullable=False),
        sa.Column("currency", sa.String(10), server_default="INR"), sa.Column("effective_from", sa.Date()), sa.Column("effective_to", sa.Date()),
        sa.Column("minimum_order_qty", sa.Numeric(18,3), server_default="0"), sa.Column("freight_basis", sa.String(80)),
        sa.Column("status", sa.String(30), server_default="active"))
    op.create_table("product_cost_records", *common(),
        sa.Column("product_code", sa.String(80), nullable=False), sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("rm_cost_per_mt", sa.Numeric(18,2), nullable=False), sa.Column("manufacturing_overhead_pct", sa.Numeric(8,3), nullable=False))
    op.create_table("transport_rate_records", *common(),
        sa.Column("transport_rate_id", sa.String(80), nullable=False), sa.Column("destination_city", sa.String(120), nullable=False),
        sa.Column("destination_state", sa.String(120)), sa.Column("zone", sa.String(80), nullable=False),
        sa.Column("distance_km", sa.Numeric(12,2), server_default="0"), sa.Column("vehicle_type", sa.String(80)),
        sa.Column("rate_per_mt_inr", sa.Numeric(18,2), nullable=False), sa.Column("minimum_charge_inr", sa.Numeric(18,2), server_default="0"),
        sa.Column("handling_charge_inr", sa.Numeric(18,2), server_default="0"), sa.Column("estimated_transit_days", sa.Integer(), server_default="0"),
        sa.Column("preferred_transporter", sa.String(255)), sa.Column("status", sa.String(30), server_default="active"))
    op.create_table("discount_band_records", *common(),
        sa.Column("customer_type", sa.String(80), nullable=False), sa.Column("order_value_min", sa.Numeric(18,2), nullable=False),
        sa.Column("order_value_max", sa.Numeric(18,2), nullable=False), sa.Column("max_discount_pct", sa.Numeric(8,3), nullable=False),
        sa.Column("approval_limit_pct", sa.Numeric(8,3), nullable=False))
    op.create_table("margin_rule_records", *common(),
        sa.Column("rule_id", sa.String(80), nullable=False), sa.Column("product_code", sa.String(80)),
        sa.Column("product_category", sa.String(120)), sa.Column("minimum_margin_pct", sa.Numeric(8,3), nullable=False),
        sa.Column("target_margin_pct", sa.Numeric(8,3), nullable=False), sa.Column("stretch_margin_pct", sa.Numeric(8,3), server_default="0"),
        sa.Column("exception_approver", sa.String(120)), sa.Column("exception_rule", sa.Text()),
        sa.Column("effective_from", sa.Date()), sa.Column("status", sa.String(30), server_default="active"))
    op.create_table("gst_rate_records", *common(),
        sa.Column("gst_rule_id", sa.String(80), nullable=False), sa.Column("product_code", sa.String(80)),
        sa.Column("product_category", sa.String(120)), sa.Column("hsn_code", sa.String(30)),
        sa.Column("gst_rate_pct", sa.Numeric(8,3), nullable=False), sa.Column("cgst_pct", sa.Numeric(8,3), server_default="0"),
        sa.Column("sgst_pct", sa.Numeric(8,3), server_default="0"), sa.Column("igst_pct", sa.Numeric(8,3), server_default="0"),
        sa.Column("effective_from", sa.Date()), sa.Column("status", sa.String(30), server_default="active"))
    op.create_table("payment_term_rule_records", *common(),
        sa.Column("term_id", sa.String(80), nullable=False), sa.Column("customer_type", sa.String(80), nullable=False),
        sa.Column("minimum_order_value_inr", sa.Numeric(18,2), nullable=False), sa.Column("maximum_order_value_inr", sa.Numeric(18,2), nullable=False),
        sa.Column("advance_percentage", sa.Numeric(8,3), nullable=False), sa.Column("credit_days", sa.Integer(), server_default="0"),
        sa.Column("balance_payment_condition", sa.Text()), sa.Column("late_payment_interest_pct_pa", sa.Numeric(8,3), server_default="0"),
        sa.Column("exception_approver", sa.String(120)), sa.Column("status", sa.String(30), server_default="active"))
    op.create_table("customer_import_staging", *common(),
        sa.Column("external_customer_id", sa.String(100), nullable=False),
        sa.Column("resolved_customer_id", sa.String(36), sa.ForeignKey("customers.id")),
        sa.Column("company_name", sa.String(255), nullable=False), sa.Column("contact_person", sa.String(255)),
        sa.Column("phone", sa.String(50)), sa.Column("email", sa.String(255)), sa.Column("city", sa.String(120)),
        sa.Column("state", sa.String(120)), sa.Column("customer_type", sa.String(80)), sa.Column("gstin", sa.String(30)),
        sa.Column("credit_limit_inr", sa.Numeric(18,2), server_default="0"), sa.Column("outstanding_amount_inr", sa.Numeric(18,2), server_default="0"),
        sa.Column("payment_behavior", sa.String(50)), sa.Column("previous_orders_count", sa.Integer(), server_default="0"),
        sa.Column("lifetime_sales_inr", sa.Numeric(18,2), server_default="0"), sa.Column("lead_source", sa.String(80)),
        sa.Column("status", sa.String(30), server_default="active"), sa.Column("resolution_status", sa.String(30), server_default="pending"))

    op.add_column("customers", sa.Column("state", sa.String(100)))
    op.add_column("customers", sa.Column("customer_type", sa.String(80)))
    op.add_column("customers", sa.Column("imported_previous_orders_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("customers", sa.Column("imported_lifetime_sales", sa.Numeric(18,2), nullable=False, server_default="0"))
    for table in ("order_history", "quotation_history", "payment_records"):
        op.add_column(table, sa.Column("source_document_id", sa.String(36), sa.ForeignKey("business_documents.id", ondelete="SET NULL")))
        op.create_index(f"ix_{table}_source_document_id", table, ["source_document_id"])
    op.create_index("ix_customers_customer_type", "customers", ["customer_type"])
    for table in ("catalog_products", "inventory_records", "production_capacity_records", "delivery_zone_records", "product_price_records", "product_cost_records", "transport_rate_records", "discount_band_records", "margin_rule_records", "gst_rate_records", "payment_term_rule_records", "customer_import_staging"):
        op.create_index(f"ix_{table}_tenant_active", table, ["business_id", "is_active"])


def downgrade() -> None:
    for table in ("order_history", "quotation_history", "payment_records"):
        op.drop_index(f"ix_{table}_source_document_id", table_name=table)
        op.drop_column(table, "source_document_id")
    op.drop_index("ix_customers_customer_type", table_name="customers")
    for column in ("imported_lifetime_sales", "imported_previous_orders_count", "customer_type", "state"):
        op.drop_column("customers", column)
    for table in ("customer_import_staging", "payment_term_rule_records", "gst_rate_records", "margin_rule_records", "discount_band_records", "transport_rate_records", "product_cost_records", "product_price_records", "delivery_zone_records", "production_capacity_records", "inventory_records", "catalog_products"):
        op.drop_table(table)
    op.drop_table("business_documents")
