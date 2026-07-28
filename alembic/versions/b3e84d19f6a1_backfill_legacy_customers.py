"""backfill legacy customers

Revision ID: b3e84d19f6a1
Revises: 9f1c7a42d8b0
"""

from typing import Sequence, Union

from alembic import op


revision: str = "b3e84d19f6a1"
down_revision: Union[str, Sequence[str], None] = "9f1c7a42d8b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use the earliest lead UUID as the permanent customer UUID for each
    # tenant/company pair. Blank company names remain isolated per lead rather
    # than being incorrectly merged into one "Unknown" customer.
    op.execute(
        """
        WITH ranked_leads AS (
            SELECT
                l.*,
                CASE
                    WHEN NULLIF(trim(l.company_name), '') IS NOT NULL
                        THEN lower(trim(l.company_name))
                    WHEN NULLIF(trim(l.customer_name), '') IS NOT NULL
                        THEN lower(trim(l.customer_name))
                    ELSE '__unknown__:' || l.id
                END AS identity_key,
                row_number() OVER (
                    PARTITION BY
                        l.business_id,
                        CASE
                            WHEN NULLIF(trim(l.company_name), '') IS NOT NULL
                                THEN lower(trim(l.company_name))
                            WHEN NULLIF(trim(l.customer_name), '') IS NOT NULL
                                THEN lower(trim(l.customer_name))
                            ELSE '__unknown__:' || l.id
                        END
                    ORDER BY l.created_at, l.id
                ) AS identity_rank
            FROM leads AS l
            WHERE l.customer_id IS NULL
        )
        INSERT INTO customers (
            id,
            business_id,
            company_name,
            contact_person,
            email,
            credit_limit,
            outstanding_amount,
            payment_behavior,
            created_at,
            updated_at
        )
        SELECT
            r.id,
            r.business_id,
            COALESCE(
                NULLIF(trim(r.company_name), ''),
                NULLIF(trim(r.customer_name), ''),
                'Unknown customer ' || left(r.id, 8)
            ),
            r.contact_person,
            CASE
                WHEN r.sender_identifier LIKE '%@%'
                    THEN lower(trim(r.sender_identifier))
                ELSE NULL
            END,
            0,
            0,
            'UNKNOWN',
            r.created_at,
            r.updated_at
        FROM ranked_leads AS r
        WHERE r.identity_rank = 1
          AND NOT EXISTS (
              SELECT 1
              FROM customers AS c
              WHERE c.id = r.id
          )
        """
    )

    op.execute(
        """
        UPDATE leads AS l
        SET customer_id = c.id
        FROM customers AS c
        WHERE l.customer_id IS NULL
          AND l.business_id = c.business_id
          AND (
              (
                  NULLIF(trim(l.company_name), '') IS NOT NULL
                  AND lower(trim(l.company_name)) = lower(trim(c.company_name))
              )
              OR (
                  NULLIF(trim(l.company_name), '') IS NULL
                  AND NULLIF(trim(l.customer_name), '') IS NOT NULL
                  AND lower(trim(l.customer_name)) = lower(trim(c.company_name))
              )
              OR c.id = l.id
          )
        """
    )

    op.execute(
        """
        UPDATE quotations AS q
        SET customer_id = l.customer_id
        FROM leads AS l
        WHERE q.inquiry_id = l.inquiry_id
          AND q.business_id = l.business_id
        """
    )
    op.execute(
        """
        UPDATE quotation_versions AS v
        SET customer_id = q.customer_id
        FROM quotations AS q
        WHERE v.business_id = q.business_id
          AND (
              v.quotation_id = q.id
              OR v.quotation_number = q.quotation_number
          )
        """
    )
    op.execute(
        """
        UPDATE followup_records AS f
        SET customer_id = l.customer_id
        FROM leads AS l
        WHERE f.inquiry_id = l.inquiry_id
          AND f.business_id = l.business_id
        """
    )
    op.execute(
        """
        UPDATE purchase_orders AS p
        SET customer_id = l.customer_id
        FROM leads AS l
        WHERE p.inquiry_id = l.inquiry_id
          AND p.business_id = l.business_id
        """
    )
    op.execute(
        """
        UPDATE sales_orders AS s
        SET customer_id = p.customer_id
        FROM purchase_orders AS p
        WHERE s.po_id = p.id
          AND s.business_id = p.business_id
        """
    )
    op.execute(
        """
        UPDATE handoff_records AS h
        SET customer_id = s.customer_id
        FROM sales_orders AS s
        WHERE h.sales_order_id = s.id
          AND h.business_id = s.business_id
        """
    )


def downgrade() -> None:
    # Remove only customers created from a lead UUID by this migration.
    op.execute(
        """
        UPDATE handoff_records SET customer_id = NULL
        WHERE customer_id IN (SELECT id FROM leads)
        """
    )
    op.execute(
        """
        UPDATE sales_orders SET customer_id = NULL
        WHERE customer_id IN (SELECT id FROM leads)
        """
    )
    op.execute(
        """
        UPDATE purchase_orders SET customer_id = NULL
        WHERE customer_id IN (SELECT id FROM leads)
        """
    )
    op.execute(
        """
        UPDATE followup_records SET customer_id = NULL
        WHERE customer_id IN (SELECT id FROM leads)
        """
    )
    op.execute(
        """
        UPDATE quotation_versions SET customer_id = NULL
        WHERE customer_id IN (SELECT id FROM leads)
        """
    )
    op.execute(
        """
        UPDATE quotations SET customer_id = NULL
        WHERE customer_id IN (SELECT id FROM leads)
        """
    )
    op.execute(
        """
        UPDATE leads SET customer_id = NULL
        WHERE customer_id IN (SELECT id FROM leads)
        """
    )
    op.execute(
        """
        DELETE FROM customers
        WHERE id IN (SELECT id FROM leads)
        """
    )
