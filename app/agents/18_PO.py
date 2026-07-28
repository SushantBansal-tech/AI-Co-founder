"""
Sub-problem: Purchase Order Extractor

Responsibilities:
  1. Accept raw PO text (email body, copy-pasted PDF, CRM export)
  2. Extract every PO field via Gemini structured JSON output
  3. Flag which critical fields are missing
  4. Persist to purchase_orders table
  5. Return POExtraction Pydantic model for validator

Fields extracted:
  po_number, po_date, buyer_company, buyer_gstin,
  billing_address, shipping_address,
  product_description, product_code, quantity, unit,
  price_per_unit_ex_gst, gst_rate_pct, gst_amount,
  total_amount_inc_gst, payment_terms, delivery_date,
  delivery_location, special_conditions

Design rule: LLM for extraction only. Every downstream decision is deterministic.

Run:
    GEMINI_API_KEY=xxx python 18_po_extractor.py
"""

import uuid
import json
import sys
import os
import asyncio
from datetime import datetime
from typing import Optional
from importlib import import_module

from pydantic import BaseModel, Field
from google import genai
from sqlalchemy import String, Text, Numeric, DateTime, JSON, Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)
from app.database.base import Base
from app.database.models.order import (
    POStatus,
    PurchaseOrder,
    SalesOrder,
)

sys.path.insert(0, os.path.dirname(__file__))
ia = import_module("01_Inquiry")
#Base       = ia.Base
log_action = ia.log_action


# ── DB model ──────────────────────────────────────────────────────────────

# class POStatus(str):
#     PENDING  = "pending"
#     VALID    = "valid"
#     MISMATCH = "mismatch_found"
#     CORRECTED = "corrected"
#     CONFIRMED = "confirmed"


# class PurchaseOrder(Base):
#     __tablename__ = "purchase_orders"

#     id: Mapped[str]           = mapped_column(String(36), primary_key=True,
#                                                default=lambda: str(uuid.uuid4()))
#     inquiry_id: Mapped[Optional[str]]    = mapped_column(String(36), nullable=True, index=True)
#     quotation_id: Mapped[Optional[str]]  = mapped_column(String(36), nullable=True)
#     quotation_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

#     # Extracted PO fields
#     po_number: Mapped[Optional[str]]          = mapped_column(String(100), nullable=True)
#     po_date: Mapped[Optional[str]]            = mapped_column(String(50), nullable=True)
#     buyer_company: Mapped[Optional[str]]      = mapped_column(String(255), nullable=True)
#     buyer_gstin: Mapped[Optional[str]]        = mapped_column(String(20), nullable=True)
#     billing_address: Mapped[Optional[str]]    = mapped_column(Text, nullable=True)
#     shipping_address: Mapped[Optional[str]]   = mapped_column(Text, nullable=True)
#     product_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
#     product_code: Mapped[Optional[str]]       = mapped_column(String(50), nullable=True)
#     quantity: Mapped[Optional[float]]         = mapped_column(nullable=True)
#     unit: Mapped[Optional[str]]               = mapped_column(String(20), nullable=True)
#     price_per_unit_ex_gst: Mapped[Optional[float]] = mapped_column(nullable=True)
#     gst_rate_pct: Mapped[Optional[float]]     = mapped_column(nullable=True)
#     gst_amount: Mapped[Optional[float]]       = mapped_column(nullable=True)
#     total_amount_inc_gst: Mapped[Optional[float]] = mapped_column(nullable=True)
#     payment_terms: Mapped[Optional[str]]      = mapped_column(Text, nullable=True)
#     delivery_date: Mapped[Optional[str]]      = mapped_column(String(100), nullable=True)
#     delivery_location: Mapped[Optional[str]]  = mapped_column(String(255), nullable=True)
#     special_conditions: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)

#     # Validation
#     status: Mapped[str]           = mapped_column(String(30), default=POStatus.PENDING)
#     mismatches_json: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
#     missing_critical_fields: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
#     extraction_confidence: Mapped[float]  = mapped_column(default=0.0)

#     raw_po_text: Mapped[str]      = mapped_column(Text)
#     created_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
#     updated_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow,
             #                                      onupdate=datetime.utcnow)


# ── SalesOrder DB model (created when PO is confirmed) ───────────────────

# class SalesOrder(Base):
#     __tablename__ = "sales_orders"

#     id: Mapped[str]           = mapped_column(String(36), primary_key=True,
#                                                default=lambda: str(uuid.uuid4()))
#     inquiry_id: Mapped[str]   = mapped_column(String(36), index=True)
#     quotation_id: Mapped[str] = mapped_column(String(36))
#     po_id: Mapped[str]        = mapped_column(String(36), index=True)
#     po_number: Mapped[str]    = mapped_column(String(100))
#     buyer_company: Mapped[str] = mapped_column(String(255))
#     product_code: Mapped[Optional[str]]   = mapped_column(String(50), nullable=True)
#     product_description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
#     quantity: Mapped[Optional[float]]     = mapped_column(nullable=True)
#     unit: Mapped[Optional[str]]           = mapped_column(String(20), nullable=True)
#     total_value: Mapped[Optional[float]]  = mapped_column(nullable=True)
#     delivery_date: Mapped[Optional[str]]  = mapped_column(String(100), nullable=True)
#     delivery_location: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
#     payment_terms: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
#     special_notes: Mapped[Optional[str]]  = mapped_column(Text, nullable=True)
#     status: Mapped[str]       = mapped_column(String(30), default="confirmed")
#     created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Pydantic extraction model ─────────────────────────────────────────────

CRITICAL_PO_FIELDS = [
    "po_number", "buyer_company", "product_description",
    "quantity", "price_per_unit_ex_gst", "gst_rate_pct",
]

class POExtraction(BaseModel):
    po_number: Optional[str]             = None
    po_date: Optional[str]               = None
    buyer_company: Optional[str]         = None
    buyer_gstin: Optional[str]           = None
    billing_address: Optional[str]       = None
    shipping_address: Optional[str]      = None
    product_description: Optional[str]   = None
    product_code: Optional[str]          = None
    quantity: Optional[float]            = None
    unit: Optional[str]                  = None
    price_per_unit_ex_gst: Optional[float] = None
    gst_rate_pct: Optional[float]        = None
    gst_amount: Optional[float]          = None
    total_amount_inc_gst: Optional[float] = None
    payment_terms: Optional[str]         = None
    delivery_date: Optional[str]         = None
    delivery_location: Optional[str]     = None
    special_conditions: list[str]        = Field(default_factory=list)
    extraction_confidence: float         = Field(ge=0.0, le=1.0, default=0.0)
    missing_critical_fields: list[str]   = Field(default_factory=list)
    raw_text: str                        = ""


# ── Extraction prompt ─────────────────────────────────────────────────────

PO_EXTRACTION_PROMPT = """
You are extracting structured data from an industrial B2B Purchase Order (PO) document.

PO text:
---
{po_text}
---

Extract every available field. If a field is not present, return null.
For quantity, return only the numeric value (e.g., 500 for "500 MT").
For prices and amounts, return only numbers without currency symbols.
For special_conditions, return a list of strings — one per special clause or condition.

Respond ONLY with valid JSON matching this exact schema:
{{
  "po_number": "<string or null>",
  "po_date": "<string or null — e.g. 2025-06-15>",
  "buyer_company": "<string or null>",
  "buyer_gstin": "<string or null>",
  "billing_address": "<full address as string or null>",
  "shipping_address": "<full address as string or null>",
  "product_description": "<string or null>",
  "product_code": "<string or null>",
  "quantity": <number or null>,
  "unit": "<MT / NOS / KG / etc. or null>",
  "price_per_unit_ex_gst": <number or null>,
  "gst_rate_pct": <number or null>,
  "gst_amount": <number or null>,
  "total_amount_inc_gst": <number or null>,
  "payment_terms": "<string or null>",
  "delivery_date": "<string or null>",
  "delivery_location": "<string or null>",
  "special_conditions": ["<condition 1>", "<condition 2>"],
  "extraction_confidence": <0.0 to 1.0>
}}
"""


def extract_po_fields(
    po_text: str,
    client: Optional[genai.Client],
) -> POExtraction:
    """
    Extracts all PO fields via Gemini structured output.
    Falls back to an empty extraction (all nulls) if no client.
    """
    if not client:
        return POExtraction(
            raw_text=po_text,
            extraction_confidence=0.0,
            missing_critical_fields=CRITICAL_PO_FIELDS,
        )

    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=PO_EXTRACTION_PROMPT.format(po_text=po_text),
            config={"response_mime_type": "application/json"},
        )
        data = json.loads(response.text)

        # Compute missing critical fields
        missing = [
            f for f in CRITICAL_PO_FIELDS
            if not data.get(f)
        ]
        data["missing_critical_fields"] = missing
        data["raw_text"] = po_text

        return POExtraction(**data)

    except Exception as e:
        return POExtraction(
            raw_text=po_text,
            extraction_confidence=0.0,
            missing_critical_fields=CRITICAL_PO_FIELDS,
        )


# ── DB persistence ────────────────────────────────────────────────────────

async def save_po_to_db(
    session: AsyncSession,
    extraction: POExtraction,
    quotation_id: Optional[str] = None,
    quotation_number: Optional[str] = None,
    inquiry_id: Optional[str] = None,
    business_id: str = "demo-steel-company",
    customer_id: Optional[str] = None,
    thread_id: str = "",
) -> PurchaseOrder:
    po = PurchaseOrder(
        business_id=business_id,
        customer_id=customer_id,
        thread_id=thread_id or inquiry_id or str(uuid.uuid4()),
        inquiry_id=inquiry_id,
        quotation_id=quotation_id,
        quotation_number=quotation_number,
        po_number=extraction.po_number,
        po_date=extraction.po_date,
        buyer_company=extraction.buyer_company,
        buyer_gstin=extraction.buyer_gstin,
        billing_address=extraction.billing_address,
        shipping_address=extraction.shipping_address,
        product_description=extraction.product_description,
        product_code=extraction.product_code,
        quantity=extraction.quantity,
        unit=extraction.unit,
        price_per_unit_ex_gst=extraction.price_per_unit_ex_gst,
        gst_rate_pct=extraction.gst_rate_pct,
        gst_amount=extraction.gst_amount,
        total_amount_inc_gst=extraction.total_amount_inc_gst,
        payment_terms=extraction.payment_terms,
        delivery_date=extraction.delivery_date,
        delivery_location=extraction.delivery_location,
        special_conditions=extraction.special_conditions,
        extraction_confidence=extraction.extraction_confidence,
        missing_critical_fields=extraction.missing_critical_fields,
        raw_po_text=extraction.raw_text,
        status=POStatus.PENDING,
    )
    session.add(po)
    await session.flush()
    await log_action(
        session, "purchase_order", po.id, "po_received", "po_agent",
        {
            "po_number": extraction.po_number,
            "buyer":     extraction.buyer_company,
            "quantity":  extraction.quantity,
            "amount":    extraction.total_amount_inc_gst,
            "missing":   extraction.missing_critical_fields,
        },
    )
    await session.commit()
    return po


# ── Demo ──────────────────────────────────────────────────────────────────

SAMPLE_PO_TEXT = """
PURCHASE ORDER

PO Number   : APX-PO-2025-0891
PO Date     : 15-06-2025
Valid Until : 30-06-2025

From:
  Apex Steel Pvt Ltd
  Plot 22, Industrial Area, Ludhiana - 141003, Punjab
  GSTIN: 03AABCA1234C1Z5
  Contact: Ramesh Kumar | +91-9812345678

To:
  IndusSteel Trading Pvt. Ltd.
  Plot 14, Industrial Area Phase II, Ludhiana - 141003

Bill To:  Apex Steel Pvt Ltd, Plot 22, Ludhiana (same as above)
Ship To:  Apex Steel Works, Village Sahnewal, Ludhiana - 141120

Item Details:
  Product   : MS Billet IS2062 Grade, 100x100mm Square Section
  Qty       : 500 MT (Five Hundred Metric Tons)
  Rate      : INR 14,000 per MT (ex-GST)
  GST       : 18% IGST
  GST Amount: INR 12,60,000
  Total     : INR 82,60,000 (Eighty Two Lakhs Sixty Thousand only)

Payment Terms : 20% advance with PO, balance 80% within 45 days of dispatch
Delivery Date : On or before 30-06-2025
Delivery Loc  : Sahnewal, Ludhiana

Special Conditions:
  1. Material must be accompanied by original mill test certificate.
  2. Each bundle to be labelled with heat number and grade.
  3. Packaging: bundles of 2 MT each.
  4. Shortfall of more than 1% in delivered quantity will attract penalty.

Authorised Signatory: Ramesh Kumar, Procurement Head
"""

if __name__ == "__main__":
    async def _demo():
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
                 if os.environ.get("GEMINI_API_KEY") else None

        extraction = extract_po_fields(SAMPLE_PO_TEXT, client)

        print("PO EXTRACTION RESULT")
        print(f"{'='*55}")
        print(f"PO Number         : {extraction.po_number}")
        print(f"PO Date           : {extraction.po_date}")
        print(f"Buyer             : {extraction.buyer_company}")
        print(f"Buyer GSTIN       : {extraction.buyer_gstin}")
        print(f"Product           : {extraction.product_description}")
        print(f"Quantity          : {extraction.quantity} {extraction.unit}")
        print(f"Price ex-GST      : ₹{extraction.price_per_unit_ex_gst:,.0f}/MT"
              if extraction.price_per_unit_ex_gst else "Price: not found")
        print(f"GST Rate          : {extraction.gst_rate_pct}%")
        print(f"GST Amount        : ₹{extraction.gst_amount:,.0f}"
              if extraction.gst_amount else "GST: not found")
        print(f"Total inc-GST     : ₹{extraction.total_amount_inc_gst:,.0f}"
              if extraction.total_amount_inc_gst else "Total: not found")
        print(f"Payment Terms     : {extraction.payment_terms}")
        print(f"Delivery Date     : {extraction.delivery_date}")
        print(f"Delivery Location : {extraction.delivery_location}")
        print(f"Billing Address   : {extraction.billing_address}")
        print(f"Shipping Address  : {extraction.shipping_address}")
        print(f"\nSpecial Conditions:")
        for i, sc in enumerate(extraction.special_conditions, 1):
            print(f"  {i}. {sc}")
        print(f"\nConfidence        : {extraction.extraction_confidence:.0%}")
        print(f"Missing critical  : {extraction.missing_critical_fields or 'None'}")

        # Persist
        engine = create_async_engine("sqlite+aiosqlite:///sales_os.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with Session() as session:
            po_record = await save_po_to_db(
                session, extraction,
                quotation_id="Q-DEMO-001",
                quotation_number="QT-2025-A1B2",
                inquiry_id="INQ-001",
            )
        print(f"\nDB record saved   : {po_record.id}")
        print(f"Status            : {po_record.status}")

    asyncio.run(_demo())
