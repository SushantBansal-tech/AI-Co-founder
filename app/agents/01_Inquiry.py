import os
import json
import uuid
import asyncio
from datetime import datetime
from enum import Enum
from typing import Optional
from dotenv import load_dotenv

from pydantic import BaseModel, Field
from sqlalchemy import String, Text, JSON, DateTime, Enum as SAEnum
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from google import genai

load_dotenv()  # Load environment variables from .env file


# ============================================================
# 1. NORMALIZED INQUIRY INPUT
# ============================================================

class InquirySource(str, Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"
    WEB_FORM = "web_form"
    CRM = "crm"
    UPLOADED_DOCUMENT = "uploaded_document"


class RawInquiry(BaseModel):
    inquiry_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: InquirySource
    raw_text: str
    sender_identifier: Optional[str] = None
    received_at: datetime = Field(default_factory=datetime.utcnow)


def normalize_inquiry(
    source: InquirySource,
    raw_text: str,
    sender_identifier: Optional[str] = None
) -> RawInquiry:
    cleaned = raw_text.strip()
    return RawInquiry(
        source=source,
        raw_text=cleaned,
        sender_identifier=sender_identifier
    )


# ============================================================
# 2. GEMINI STRUCTURED EXTRACTION
# ============================================================

class InquiryExtraction(BaseModel):
    inquiry_id: str
    customer_name: Optional[str] = None
    company_name: Optional[str] = None
    contact_person: Optional[str] = None
    product_requested: Optional[str] = None
    quantity: Optional[str] = None
    specifications: Optional[str] = None
    delivery_location: Optional[str] = None
    delivery_date: Optional[str] = None
    payment_expectation: Optional[str] = None
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    missing_fields: list[str] = Field(default_factory=list)


REQUIRED_FIELDS = [
    "customer_name",
    "company_name",
    "product_requested",
    "quantity",
    "delivery_location",
]


EXTRACTION_PROMPT = """
You are extracting structured data from an industrial B2B sales inquiry.

Extract only what is clearly present in the text.
Do not guess or invent missing values.

Inquiry text:
---
{raw_text}
---
"""


def extract_inquiry(raw: RawInquiry, client: genai.Client) -> InquiryExtraction:
    response = client.models.generate_content(
        model="gemini-3.1-flash-lite",
        contents=EXTRACTION_PROMPT.format(raw_text=raw.raw_text),
        config={
            "response_mime_type": "application/json",
            "response_schema": {
                "type": "object",
                "properties": {
                    "customer_name": {"type": "string", "nullable": True},
                    "company_name": {"type": "string", "nullable": True},
                    "contact_person": {"type": "string", "nullable": True},
                    "product_requested": {"type": "string", "nullable": True},
                    "quantity": {"type": "string", "nullable": True},
                    "specifications": {"type": "string", "nullable": True},
                    "delivery_location": {"type": "string", "nullable": True},
                    "delivery_date": {"type": "string", "nullable": True},
                    "payment_expectation": {"type": "string", "nullable": True},
                    "extraction_confidence": {"type": "number"},
                },
                "required": ["extraction_confidence"],
            },
        },
    )

    data = json.loads(response.text)
    data["inquiry_id"] = raw.inquiry_id
    data["missing_fields"] = [field for field in REQUIRED_FIELDS if not data.get(field)]

    return InquiryExtraction(**data)


# ============================================================
# 3. FOLLOW-UP MESSAGE GENERATION
# ============================================================

FIELD_QUESTIONS = {
    "customer_name": "Could you confirm your name?",
    "company_name": "Could you share your company name?",
    "product_requested": "Could you confirm the exact product or grade you need?",
    "quantity": "What quantity do you require?",
    "delivery_location": "Where should this be delivered?",
}


class FollowUpMessage(BaseModel):
    inquiry_id: str
    channel: InquirySource
    questions: list[str]
    message_text: str


def generate_followup_questions(extraction: InquiryExtraction) -> list[str]:
    return [
        FIELD_QUESTIONS[field]
        for field in extraction.missing_fields
        if field in FIELD_QUESTIONS
    ]


def compose_followup_message(
    extraction: InquiryExtraction,
    raw: RawInquiry,
    client: Optional[genai.Client] = None
) -> Optional[FollowUpMessage]:

    questions = generate_followup_questions(extraction)

    if not questions:
        return None

    greeting = f"Hi {extraction.customer_name or ''}".strip() + ","

    fallback_text = (
        greeting
        + "\n\nThanks for your inquiry. To prepare an accurate quote, "
        + "could you please confirm:\n"
        + "\n".join(f"- {q}" for q in questions)
        + "\n\nRegards."
    )

    if client is None:
        message_text = fallback_text
    else:
        tone = (
            "a short, friendly WhatsApp message"
            if raw.source == InquirySource.WHATSAPP
            else "a professional email reply"
        )

        prompt = (
            f"Rewrite these questions as {tone}. "
            f"Ask the customer to confirm the missing details before quote preparation. "
            f"Do not invent information.\n\n"
            f"Questions:\n" + "\n".join(questions)
        )

        try:
            response = client.models.generate_content(
                model="gemini-3.1-flash-lite",
                contents=prompt
            )
            message_text = response.text.strip()
        except Exception:
            message_text = fallback_text

    return FollowUpMessage(
        inquiry_id=extraction.inquiry_id,
        channel=raw.source,
        questions=questions,
        message_text=message_text,
    )


# ============================================================
# 4. DATABASE MODELS
# ============================================================

class LeadStatus(str, Enum):
    AWAITING_INFO = "awaiting_info"
    NEW = "new"
    WON = "won"


class Base(DeclarativeBase):
    pass


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4())
    )

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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4())
    )

    entity_type: Mapped[str] = mapped_column(String(50))
    entity_id: Mapped[str] = mapped_column(String(36), index=True)
    action: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def log_action(
    session: AsyncSession,
    entity_type: str,
    entity_id: str,
    action: str,
    actor: str,
    details: dict
) -> None:
    session.add(
        AuditLog(
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            actor=actor,
            details=details,
        )
    )


async def create_lead(
    session: AsyncSession,
    raw: RawInquiry,
    extraction: InquiryExtraction
) -> Lead:

    status = (
        LeadStatus.AWAITING_INFO
        if extraction.missing_fields
        else LeadStatus.NEW
    )

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
    await session.flush()

    await log_action(
        session=session,
        entity_type="lead",
        entity_id=lead.id,
        action="lead_created",
        actor="inquiry_agent",
        details={
            "status": status.value,
            "missing_fields": extraction.missing_fields,
            "extraction_confidence": extraction.extraction_confidence,
        },
    )

    await session.commit()
    return lead


# ============================================================
# 5. FULL PIPELINE
# ============================================================

async def run_pipeline():
    api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        raise ValueError("Please set GEMINI_API_KEY in your environment.")

    client = genai.Client(api_key=api_key)

    # For persistent local database, use:
    # sqlite+aiosqlite:///sales_os.db
    engine = create_async_engine("sqlite+aiosqlite:///sales_os.db")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False
    )

    raw = normalize_inquiry(
        source=InquirySource.WHATSAPP,
        raw_text="""
        Need 200 units of MS pipe, 2 inch dia. ASAP please.
        """,
        sender_identifier="+919812345678",
    )

    extraction = extract_inquiry(raw, client)

    print("\n--- Extraction Result ---")
    print(extraction.model_dump_json(indent=2))

    followup = compose_followup_message(
        extraction=extraction,
        raw=raw,
        client=client,
    )

    print("\n--- Follow-up Message ---")
    if followup:
        print(followup.message_text)
    else:
        print("No follow-up needed. All required fields are available.")

    async with Session() as session:
        lead = await create_lead(session, raw, extraction)

    print("\n--- Lead Persisted ---")
    print(f"Lead ID: {lead.id}")
    print(f"Status: {lead.status.value}")
    print(f"Missing Fields: {lead.missing_fields}")


if __name__ == "__main__":
    asyncio.run(run_pipeline())
