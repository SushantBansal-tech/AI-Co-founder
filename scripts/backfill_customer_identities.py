import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.customers.normalization import (
    normalize_company_name,
    normalize_email,
    normalize_email_domain,
    normalize_gstin,
    normalize_phone,
)
from app.database import (
    Customer,
    CustomerIdentity,
    CustomerMatchReview,
    CustomerMatchReviewStatus,
    SessionFactory,
)


async def main() -> None:
    created = 0
    skipped = 0
    async with SessionFactory() as session:
        customers = (await session.execute(select(Customer))).scalars().all()

        for customer in customers:
            candidates = [
                ("company_name", customer.company_name, normalize_company_name(customer.company_name), False),
                ("email", customer.email, normalize_email(customer.email), True),
                ("email_domain", customer.email, normalize_email_domain(customer.email), False),
                ("phone", customer.phone, normalize_phone(customer.phone), True),
                ("gstin", customer.gstin, normalize_gstin(customer.gstin), True),
            ]
            for identity_type, raw_value, normalized_value, verified in candidates:
                if not raw_value or not normalized_value:
                    continue
                existing_identity = await session.scalar(
                    select(CustomerIdentity).where(
                        CustomerIdentity.business_id == customer.business_id,
                        CustomerIdentity.identity_type == identity_type,
                        CustomerIdentity.normalized_value == normalized_value,
                    )
                )
                if existing_identity:
                    skipped += 1
                    if existing_identity.customer_id != customer.id:
                        existing_review = await session.scalar(
                            select(CustomerMatchReview.id).where(
                                CustomerMatchReview.business_id
                                == customer.business_id,
                                CustomerMatchReview.provisional_customer_id
                                == customer.id,
                                CustomerMatchReview.candidate_customer_id
                                == existing_identity.customer_id,
                                CustomerMatchReview.status
                                == CustomerMatchReviewStatus.PENDING,
                            )
                        )
                        if not existing_review:
                            customer.status = "provisional"
                            session.add(
                                CustomerMatchReview(
                                    business_id=customer.business_id,
                                    provisional_customer_id=customer.id,
                                    candidate_customer_id=(
                                        existing_identity.customer_id
                                    ),
                                    confidence=1.0,
                                    matched_signals=[
                                        f"duplicate_{identity_type}"
                                    ],
                                    conflicting_signals=[],
                                )
                            )
                    continue
                session.add(
                    CustomerIdentity(
                        business_id=customer.business_id,
                        customer_id=customer.id,
                        identity_type=identity_type,
                        raw_value=raw_value,
                        normalized_value=normalized_value,
                        is_verified=verified,
                        is_primary=True,
                        source="legacy_backfill",
                    )
                )
                created += 1

        await session.commit()
    print(f"Created {created} identities; skipped {skipped} existing identities.")


if __name__ == "__main__":
    asyncio.run(main())
