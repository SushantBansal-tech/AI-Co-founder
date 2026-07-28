"""add permanent customer linkage

Revision ID: 9f1c7a42d8b0
Revises: 03b923e42ec4
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "9f1c7a42d8b0"
down_revision: Union[str, Sequence[str], None] = "03b923e42ec4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BUSINESS_ID = "demo-steel-company"


def _add_business_id(table: str) -> None:
    op.add_column(table, sa.Column("business_id", sa.String(100), nullable=True))
    op.execute(
        sa.text(f"UPDATE {table} SET business_id = :business_id")
        .bindparams(business_id=BUSINESS_ID)
    )
    op.alter_column(table, "business_id", nullable=False)
    op.create_index(f"ix_{table}_business_id", table, ["business_id"])


def _add_lifecycle_links(table: str) -> None:
    op.add_column(table, sa.Column("customer_id", sa.String(36), nullable=True))
    op.add_column(table, sa.Column("thread_id", sa.String(100), nullable=True))
    op.create_index(f"ix_{table}_customer_id", table, ["customer_id"])
    op.create_index(f"ix_{table}_thread_id", table, ["thread_id"])
    op.create_foreign_key(
        f"fk_{table}_customer_id_customers",
        table,
        "customers",
        ["customer_id"],
        ["id"],
    )


def upgrade() -> None:
    for table in (
        "customers",
        "order_history",
        "quotation_history",
        "payment_records",
        "leads",
        "quotations",
        "quotation_versions",
        "followup_records",
        "purchase_orders",
        "sales_orders",
        "handoff_records",
    ):
        _add_business_id(table)

    op.add_column(
        "audit_logs", sa.Column("business_id", sa.String(100), nullable=True)
    )
    op.execute(
        sa.text("UPDATE audit_logs SET business_id = :business_id")
        .bindparams(business_id=BUSINESS_ID)
    )
    op.create_index("ix_audit_logs_business_id", "audit_logs", ["business_id"])

    op.add_column("leads", sa.Column("customer_id", sa.String(36), nullable=True))
    op.add_column("leads", sa.Column("thread_id", sa.String(100), nullable=True))
    op.create_index("ix_leads_customer_id", "leads", ["customer_id"])
    op.create_index("ix_leads_thread_id", "leads", ["thread_id"], unique=True)
    op.create_foreign_key(
        "fk_leads_customer_id_customers",
        "leads",
        "customers",
        ["customer_id"],
        ["id"],
    )

    for table in (
        "quotations",
        "quotation_versions",
        "followup_records",
        "purchase_orders",
        "sales_orders",
        "handoff_records",
    ):
        _add_lifecycle_links(table)

    op.add_column("audit_logs", sa.Column("customer_id", sa.String(36), nullable=True))
    op.add_column("audit_logs", sa.Column("thread_id", sa.String(100), nullable=True))
    op.create_index("ix_audit_logs_customer_id", "audit_logs", ["customer_id"])
    op.create_index("ix_audit_logs_thread_id", "audit_logs", ["thread_id"])
    op.create_foreign_key(
        "fk_audit_logs_customer_id_customers",
        "audit_logs",
        "customers",
        ["customer_id"],
        ["id"],
    )

    # A legacy row has no persisted LangGraph thread id. Its lead UUID is a
    # collision-free stable replacement; new rows store the actual thread id.
    op.execute("UPDATE leads SET thread_id = id WHERE thread_id IS NULL")

    # Match legacy leads to a customer only when company name and tenant agree.
    op.execute(
        """
        UPDATE leads AS l
        SET customer_id = c.id
        FROM customers AS c
        WHERE l.customer_id IS NULL
          AND l.business_id = c.business_id
          AND l.company_name IS NOT NULL
          AND lower(trim(l.company_name)) = lower(trim(c.company_name))
        """
    )

    op.execute(
        """
        UPDATE quotations AS q
        SET customer_id = l.customer_id,
            thread_id = l.thread_id,
            business_id = l.business_id
        FROM leads AS l
        WHERE q.inquiry_id = l.inquiry_id
        """
    )
    op.execute(
        """
        UPDATE quotation_versions AS v
        SET customer_id = q.customer_id,
            thread_id = q.thread_id,
            business_id = q.business_id
        FROM quotations AS q
        WHERE v.quotation_id = q.id
           OR v.quotation_number = q.quotation_number
        """
    )
    op.execute(
        """
        UPDATE followup_records AS f
        SET customer_id = l.customer_id,
            thread_id = l.thread_id,
            business_id = l.business_id
        FROM leads AS l
        WHERE f.inquiry_id = l.inquiry_id
        """
    )
    op.execute(
        """
        UPDATE purchase_orders AS p
        SET customer_id = l.customer_id,
            thread_id = l.thread_id,
            business_id = l.business_id
        FROM leads AS l
        WHERE p.inquiry_id = l.inquiry_id
        """
    )
    op.execute(
        """
        UPDATE sales_orders AS s
        SET customer_id = p.customer_id,
            thread_id = p.thread_id,
            business_id = p.business_id
        FROM purchase_orders AS p
        WHERE s.po_id = p.id
        """
    )
    op.execute(
        """
        UPDATE handoff_records AS h
        SET customer_id = s.customer_id,
            thread_id = s.thread_id,
            business_id = s.business_id
        FROM sales_orders AS s
        WHERE h.sales_order_id = s.id
        """
    )

    # All lifecycle rows get a stable fallback thread even when the legacy
    # relational chain is incomplete.
    for table in (
        "quotations",
        "quotation_versions",
        "followup_records",
        "purchase_orders",
        "sales_orders",
        "handoff_records",
    ):
        op.execute(f"UPDATE {table} SET thread_id = id WHERE thread_id IS NULL")
        op.alter_column(table, "thread_id", nullable=False)

    op.alter_column("leads", "thread_id", nullable=False)


def downgrade() -> None:
    for table in (
        "handoff_records",
        "sales_orders",
        "purchase_orders",
        "followup_records",
        "quotation_versions",
        "quotations",
    ):
        op.drop_constraint(
            f"fk_{table}_customer_id_customers", table, type_="foreignkey"
        )
        op.drop_index(f"ix_{table}_thread_id", table_name=table)
        op.drop_index(f"ix_{table}_customer_id", table_name=table)
        op.drop_column(table, "thread_id")
        op.drop_column(table, "customer_id")

    op.drop_constraint(
        "fk_audit_logs_customer_id_customers", "audit_logs", type_="foreignkey"
    )
    op.drop_index("ix_audit_logs_thread_id", table_name="audit_logs")
    op.drop_index("ix_audit_logs_customer_id", table_name="audit_logs")
    op.drop_column("audit_logs", "thread_id")
    op.drop_column("audit_logs", "customer_id")

    op.drop_constraint(
        "fk_leads_customer_id_customers", "leads", type_="foreignkey"
    )
    op.drop_index("ix_leads_thread_id", table_name="leads")
    op.drop_index("ix_leads_customer_id", table_name="leads")
    op.drop_column("leads", "thread_id")
    op.drop_column("leads", "customer_id")

    for table in (
        "handoff_records",
        "sales_orders",
        "purchase_orders",
        "followup_records",
        "quotation_versions",
        "quotations",
        "audit_logs",
        "leads",
        "payment_records",
        "quotation_history",
        "order_history",
        "customers",
    ):
        op.drop_index(f"ix_{table}_business_id", table_name=table)
        op.drop_column(table, "business_id")
