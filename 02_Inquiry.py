"""
Sub-problem 3: Turn InquiryExtraction.missing_fields into an actual
customer-facing follow-up question.

Sub-problem 4: Persist the extraction as a Lead record (+ audit log entry,
since every action needs one per the spec).

Depends on: 01_inquiry_extraction.py (InquiryExtraction, RawInquiry, InquirySource)

Run directly to test (uses local sqlite, swap DATABASE_URL for Postgres in prod):
    GEMINI_API_KEY=xxx python 02_followup_and_lead.py
"""

import os
import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel
from sqlalchemy import String, Text, JSON, DateTime, Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from google import genai

from importlib import import_module
inquiry_mod = import_module("01_Inquiry")
InquiryExtraction = inquiry_mod.InquiryExtraction
RawInquiry = inquiry_mod.RawInquiry
InquirySource = inquiry_mod.InquirySource


# ---------------------------------------------------------------------------
# Sub-problem 3: Follow-up question generation
# ---------------------------------------------------------------------------
# Deterministic templates per field (reliable, no hallucination risk) +
# an optional LLM pass to merge them into one natural-sounding message.
# Template generation never depends on the LLM call succeeding — if Gemini
# is unavailable, the templated questions are still a valid, sendable output.

FIELD_QUESTIONS = {
    "customer_name": "Could you confirm your name?",
    "company_name": "Could you share your company name?",
    "product_requested": "Could you confirm the exact product/grade you need?",
    "quantity": "What quantity do you require?",
    "delivery_location": "Where should this be delivered (city/plant location)?",
}


class FollowUpMessage(BaseModel):
    inquiry_id: str
    channel: InquirySource
    questions: list[str]
    message_text: str


def generate_followup_questions(extraction: InquiryExtraction) -> list[str]:
    return [FIELD_QUESTIONS[f] for f in extraction.missing_fields if f in FIELD_QUESTIONS]


def compose_followup_message(extraction: InquiryExtraction, raw: RawInquiry,
                              client: Optional[genai.Client] = None) -> Optional[FollowUpMessage]:
    """Returns None if nothing is missing — no follow-up needed."""
    questions = generate_followup_questions(extraction)
    if not questions:
        return None

    # Deterministic fallback: works even without an LLM call.
    greeting = f"Hi {extraction.customer_name or ''}".strip() + ","
    fallback_text = greeting + "\n\nThanks for your inquiry. To prepare an accurate quote, " \
        + "could you please confirm:\n" + "\n".join(f"- {q}" for q in questions) + "\n\nRegards."

    if client is None:
        message_text = fallback_text
    else:
        # Optional polish pass — tone differs for WhatsApp (short) vs email (formal).
        tone = "a short, friendly WhatsApp message (2-3 lines max)" if raw.source == InquirySource.WHATSAPP \
            else "a professional email reply"
        prompt = (
            f"Rewrite the following list of questions as {tone}, asking the customer "
            f"to confirm the missing details before we quote. Do not invent any info "
            f"that isn't in the questions.\n\nQuestions:\n" + "\n".join(questions)
        )
        try:
            response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
            message_text = response.text.strip()
        except Exception:
            message_text = fallback_text  # LLM failure must never block sending the follow-up

    return FollowUpMessage(
        inquiry_id=extraction.inquiry_id,
        channel=raw.source,
        questions=questions,
        message_text=message_text,
    )


# ---------------------------------------------------------------------------
# Sub-problem 4: Lead persistence + audit log
# ---------------------------------------------------------------------------

class LeadStatus(str, Enum):
    AWAITING_INFO = "awaiting_info"   # missing required fields, follow-up sent
    NEW = "new"                        # fully captured, ready for requirement matching


class Base(DeclarativeBase):
    pass


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    inquiry_id: Mapped[str] = mapped_column(String(36), index=True)
    source: Mapped[str] = mapped_column(SAEnum(InquirySource))
    sender_identifier: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    customer_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    company_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    contact_person: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    product_requested: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    quantity: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    specifications: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    delivery_location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    delivery_date: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    payment_expectation: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(SAEnum(LeadStatus), default=LeadStatus.NEW)
    missing_fields: Mapped[list] = mapped_column(JSON, default=list)
    raw_text: Mapped[str] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))  # which agent performed this
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def log_action(session: AsyncSession, entity_type: str, entity_id: str,
                      action: str, actor: str, details: dict) -> None:
    session.add(AuditLog(
        entity_type=entity_type, entity_id=entity_id,
        action=action, actor=actor, details=details,
    ))


async def create_lead(session: AsyncSession, raw: RawInquiry,
                       extraction: InquiryExtraction) -> Lead:
    status = LeadStatus.AWAITING_INFO if extraction.missing_fields else LeadStatus.NEW

    lead = Lead(
        inquiry_id=extraction.inquiry_id,
        source=raw.source,
        sender_identifier=raw.sender_identifier,
        customer_name=extraction.customer_name,
        company_name=extraction.company_name,
        contact_person=extraction.contact_person,
        product_requested=extraction.product_requested,
        quantity=extraction.quantity,
        specifications=extraction.specifications,
        delivery_location=extraction.delivery_location,
        delivery_date=extraction.delivery_date,
        payment_expectation=extraction.payment_expectation,
        status=status,
        missing_fields=extraction.missing_fields,
        raw_text=raw.raw_text,
    )
    session.add(lead)
    await session.flush()  # get lead.id before logging

    await log_action(
        session, entity_type="lead", entity_id=lead.id,
        action="lead_created", actor="inquiry_agent",
        details={"status": status.value, "missing_fields": extraction.missing_fields},
    )

    await session.commit()
    return lead


# ---------------------------------------------------------------------------
# Manual test
# ---------------------------------------------------------------------------

async def _demo():
    # Swap this for: postgresql+asyncpg://user:pass@host/dbname
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    # Deliberately incomplete inquiry — no delivery location given.
    raw = RawInquiry(
        source=InquirySource.WHATSAPP,
        raw_text="Need 200 units of MS pipe, 2 inch dia. ASAP please.",
        sender_identifier="+919812345678",
    )

    # Mock extraction result (in the real flow this comes from extract_inquiry()).
    extraction = InquiryExtraction(
        inquiry_id=raw.inquiry_id,
        product_requested="MS pipe 2 inch",
        quantity="200 units",
        extraction_confidence=0.8,
        missing_fields=["customer_name", "company_name", "delivery_location"],
    )

   
    followup = compose_followup_message(extraction, raw, client)
    print("--- Follow-up message ---")
    print(followup.message_text if followup else "(none needed)")

    async with Session() as session:
        lead = await create_lead(session, raw, extraction)
        print("\n--- Lead persisted ---")
        print(f"id={lead.id} status={lead.status.value} missing={lead.missing_fields}")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_demo())