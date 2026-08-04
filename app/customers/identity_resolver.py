import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.customers.normalization import (
    normalize_company_name,
    normalize_email,
    normalize_gstin,
    normalize_phone,
)
from app.database.models.customer import (
    Customer,
    CustomerIdentity,
    CustomerMatchReview,
)


EXACT_CONFIDENCE = {
    "gstin": 1.0,
    "email": 0.99,
    "phone": 0.98,
}


@dataclass
class IdentityResolution:
    customer: Customer
    resolution: str
    confidence: float
    matched_signals: list[str]
    conflicting_signals: list[str]
    review_id: Optional[str] = None


def _identity_values(
    *,
    company_name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    gstin: Optional[str],
    sender_identifier: Optional[str],
) -> dict[str, tuple[str, str, bool]]:
    normalized_email = normalize_email(email) or normalize_email(sender_identifier)
    normalized_phone = normalize_phone(phone) or normalize_phone(sender_identifier)
    normalized_gstin = normalize_gstin(gstin)
    normalized_company = normalize_company_name(company_name)

    values: dict[str, tuple[str, str, bool]] = {}
    if normalized_gstin:
        values["gstin"] = (gstin or normalized_gstin, normalized_gstin, True)
    if normalized_email:
        values["email"] = (email or sender_identifier or normalized_email, normalized_email, True)
        # domain = normalize_email_domain(normalized_email)
        # if domain:
        #     values["email_domain"] = (
        #         email or sender_identifier or normalized_email,
        #         domain,
        #         False,
        #     )
    if normalized_phone:
        values["phone"] = (phone or sender_identifier or normalized_phone, normalized_phone, True)
    if normalized_company:
        values["company_name"] = (
            company_name or normalized_company,
            normalized_company,
            False,
        )
    return values


async def _exact_matches(
    session: AsyncSession,
    business_id: str,
    values: dict[str, tuple[str, str, bool]],
) -> dict[str, list[str]]:
    matches: dict[str, list[str]] = {}
    for identity_type in ("gstin", "email", "phone"):
        identity_value = values.get(identity_type)
        if not identity_value:
            continue
        result = await session.execute(
            select(CustomerIdentity).where(
                CustomerIdentity.business_id == business_id,
                CustomerIdentity.identity_type == identity_type,
                CustomerIdentity.normalized_value == identity_value[1],
                CustomerIdentity.is_verified.is_(True),
            )
        )
        identity = result.scalar_one_or_none()
        if identity:
            matches.setdefault(identity.customer_id, []).append(identity_type)
    return matches


async def _company_candidate(
    session: AsyncSession,
    business_id: str,
    normalized_company: Optional[str],
) -> tuple[Optional[Customer], float]:
    if not normalized_company:
        return None, 0.0
    result = await session.execute(
        select(CustomerIdentity, Customer)
        .join(Customer, Customer.id == CustomerIdentity.customer_id)
        .where(
            CustomerIdentity.business_id == business_id,
            CustomerIdentity.identity_type == "company_name",
            Customer.status == "active",
        )
    )
    best_customer: Optional[Customer] = None
    best_score = 0.0
    for identity, customer in result.all():
        score = SequenceMatcher(
            None, normalized_company, identity.normalized_value
        ).ratio()
        if score > best_score:
            best_customer = customer
            best_score = score
    return (best_customer, best_score) if best_score >= 0.75 else (None, best_score)


async def _add_identities(
    session: AsyncSession,
    customer: Customer,
    values: dict[str, tuple[str, str, bool]],
    source: str,
) -> None:
    for identity_type, (
        raw,
        normalized,
        verified,
    ) in values.items():
        existing_identity = await session.scalar(
            select(CustomerIdentity).where(
                CustomerIdentity.business_id
                == customer.business_id,
                CustomerIdentity.identity_type
                == identity_type,
                CustomerIdentity.normalized_value
                == normalized,
            )
        )

        if existing_identity is not None:
            # Do not violate the unique identity constraint.
            # The identity-resolution flow decides whether the
            # customers should be matched or manually reviewed.
            continue

        session.add(
            CustomerIdentity(
                business_id=customer.business_id,
                customer_id=customer.id,
                identity_type=identity_type,
                raw_value=raw,
                normalized_value=normalized,
                is_verified=verified,
                is_primary=True,
                source=source,
            )
        )

    await session.flush()


async def _create_customer(
    session: AsyncSession,
    *,
    business_id: str,
    company_name: Optional[str],
    contact_person: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    gstin: Optional[str],
    values: dict[str, tuple[str, str, bool]],
    provisional: bool,
) -> Customer:
    customer = Customer(
        id=str(uuid.uuid4()),
        business_id=business_id,
        company_name=company_name or contact_person or "Unknown customer",
        contact_person=contact_person,
        email=normalize_email(email),
        phone=normalize_phone(phone),
        gstin=normalize_gstin(gstin),
        status="provisional" if provisional else "active",
    )
    session.add(customer)
    await session.flush()
    await _add_identities(session, customer, values, source="inquiry")
    return customer


async def resolve_customer_identity(
    session: AsyncSession,
    *,
    business_id: str,
    lead_id: Optional[str],
    company_name: Optional[str],
    contact_person: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    gstin: Optional[str],
    sender_identifier: Optional[str],
) -> IdentityResolution:
    values = _identity_values(
        company_name=company_name,
        email=email,
        phone=phone,
        gstin=gstin,
        sender_identifier=sender_identifier,
    )
    exact = await _exact_matches(session, business_id, values)

    if len(exact) == 1:
        customer_id, signals = next(iter(exact.items()))
        customer = await session.get(Customer, customer_id)
        if customer and customer.status == "active":
            return IdentityResolution(
                customer=customer,
                resolution="exact_match",
                confidence=max(EXACT_CONFIDENCE[signal] for signal in signals),
                matched_signals=signals,
                conflicting_signals=[],
            )
        if customer and customer.status == "provisional":
            review = await session.scalar(
                select(CustomerMatchReview).where(
                    CustomerMatchReview.business_id == business_id,
                    CustomerMatchReview.provisional_customer_id == customer.id,
                    CustomerMatchReview.status == "PENDING",
                )
            )
            return IdentityResolution(
                customer=customer,
                resolution="needs_review",
                confidence=max(
                    EXACT_CONFIDENCE[signal] for signal in signals
                ),
                matched_signals=signals,
                conflicting_signals=[],
                review_id=review.id if review else None,
            )

    company_value = values.get("company_name")
    candidate, company_score = await _company_candidate(
        session,
        business_id,
        company_value[1] if company_value else None,
    )
    conflict_signals = (
        ["verified_identifiers_point_to_different_customers"]
        if len(exact) > 1
        else []
    )

    if candidate or conflict_signals:
        if candidate is None and exact:
            candidate = await session.get(Customer, next(iter(exact)))
        provisional = await _create_customer(
            session,
            business_id=business_id,
            company_name=company_name,
            contact_person=contact_person,
            email=email,
            phone=phone,
            gstin=gstin,
            values={
                key: value
                for key, value in values.items()
                if key not in {"gstin", "email", "phone"} or not exact
            },
            provisional=True,
        )
        review = CustomerMatchReview(
            business_id=business_id,
            lead_id=lead_id,
            provisional_customer_id=provisional.id,
            candidate_customer_id=candidate.id,
            confidence=company_score,
            matched_signals=(
                ["similar_company_name"] if candidate else []
            ),
            conflicting_signals=conflict_signals,
        )
        session.add(review)
        await session.flush()
        return IdentityResolution(
            customer=provisional,
            resolution="needs_review",
            confidence=company_score,
            matched_signals=review.matched_signals,
            conflicting_signals=conflict_signals,
            review_id=review.id,
        )

    customer = await _create_customer(
        session,
        business_id=business_id,
        company_name=company_name,
        contact_person=contact_person,
        email=email,
        phone=phone,
        gstin=gstin,
        values=values,
        provisional=False,
    )
    return IdentityResolution(
        customer=customer,
        resolution="created",
        confidence=1.0 if values else 0.5,
        matched_signals=list(values),
        conflicting_signals=[],
    )
