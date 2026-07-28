"""add customer identity resolution

Revision ID: c71d5f28a904
Revises: b3e84d19f6a1
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c71d5f28a904"
down_revision: Union[str, Sequence[str], None] = "b3e84d19f6a1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


review_status = postgresql.ENUM(
    "PENDING",
    "MERGED",
    "KEPT_SEPARATE",
    "DISMISSED",
    name="customer_match_review_status_enum",
    create_type=False,
)


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column(
            "status",
            sa.String(length=30),
            server_default="active",
            nullable=False,
        ),
    )
    op.add_column(
        "customers",
        sa.Column(
            "merged_into_customer_id",
            sa.String(length=36),
            nullable=True,
        ),
    )
    op.create_index("ix_customers_status", "customers", ["status"])
    op.create_index(
        "ix_customers_merged_into_customer_id",
        "customers",
        ["merged_into_customer_id"],
    )
    op.create_foreign_key(
        "fk_customers_merged_into_customer_id_customers",
        "customers",
        "customers",
        ["merged_into_customer_id"],
        ["id"],
    )

    op.create_table(
        "customer_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("business_id", sa.String(length=100), nullable=False),
        sa.Column("customer_id", sa.String(length=36), nullable=False),
        sa.Column("identity_type", sa.String(length=30), nullable=False),
        sa.Column("raw_value", sa.String(length=255), nullable=False),
        sa.Column("normalized_value", sa.String(length=255), nullable=False),
        sa.Column(
            "is_verified", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "is_primary", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column(
            "source",
            sa.String(length=50),
            server_default="inquiry",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "business_id",
            "identity_type",
            "normalized_value",
            name="uq_customer_identity",
        ),
    )
    for column in (
        "business_id",
        "customer_id",
        "identity_type",
        "normalized_value",
    ):
        op.create_index(
            f"ix_customer_identities_{column}",
            "customer_identities",
            [column],
        )

    review_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "customer_match_reviews",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("business_id", sa.String(length=100), nullable=False),
        sa.Column("lead_id", sa.String(length=36), nullable=True),
        sa.Column(
            "provisional_customer_id", sa.String(length=36), nullable=False
        ),
        sa.Column("candidate_customer_id", sa.String(length=36), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("matched_signals", sa.JSON(), nullable=False),
        sa.Column("conflicting_signals", sa.JSON(), nullable=False),
        sa.Column(
            "status",
            review_status,
            server_default="PENDING",
            nullable=False,
        ),
        sa.Column("resolved_by", sa.String(length=100), nullable=True),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["candidate_customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"]),
        sa.ForeignKeyConstraint(["provisional_customer_id"], ["customers.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in (
        "business_id",
        "lead_id",
        "provisional_customer_id",
        "candidate_customer_id",
        "status",
    ):
        op.create_index(
            f"ix_customer_match_reviews_{column}",
            "customer_match_reviews",
            [column],
        )

    # Python-side defaults remain authoritative after existing rows are safe.
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
    for column in (
        "status",
        "candidate_customer_id",
        "provisional_customer_id",
        "lead_id",
        "business_id",
    ):
        op.drop_index(
            f"ix_customer_match_reviews_{column}",
            table_name="customer_match_reviews",
        )
    op.drop_table("customer_match_reviews")
    review_status.drop(op.get_bind(), checkfirst=True)

    for column in (
        "normalized_value",
        "identity_type",
        "customer_id",
        "business_id",
    ):
        op.drop_index(
            f"ix_customer_identities_{column}",
            table_name="customer_identities",
        )
    op.drop_table("customer_identities")

    op.drop_constraint(
        "fk_customers_merged_into_customer_id_customers",
        "customers",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_customers_merged_into_customer_id", table_name="customers"
    )
    op.drop_index("ix_customers_status", table_name="customers")
    op.drop_column("customers", "merged_into_customer_id")
    op.drop_column("customers", "status")
