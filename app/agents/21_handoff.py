"""
Sub-problem: Handoff Dispatcher

Responsibilities:
  1. HandoffRecord DB model — one row per department per order
  2. save_all_handoff_records() — persist every package to DB
  3. notify_department()        — mock send (swap email/Slack/ERP webhook in prod)
  4. dispatch_all()             — save + notify all 5 departments + audit log
  5. get_handoff_status()       — return current status of all department handoffs
  6. acknowledge_handoff()      — department marks their package as received

Notification channels by department (configurable in prod):
  PRODUCTION → email to production@company.com + Slack #production
  INVENTORY  → email to warehouse@company.com
  PURCHASE   → email to purchase@company.com
  DISPATCH   → email to dispatch@company.com + WhatsApp to transporter
  FINANCE    → email to accounts@company.com

Design rule: LLM used ONLY for the cover-note narrative (optional).
All DB operations and notification dispatch are deterministic.

Run:
    python 21_handoff_dispatcher.py
"""

import sys
import os
import uuid
import json
import asyncio
from datetime import datetime
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel
from google import genai
from sqlalchemy import String, Text, DateTime, JSON, select, update
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)
from app.database.base import Base
from app.database.models.handoff import (
    HandoffRecord,
    HandoffRecordStatus,
)
sys.path.insert(0, os.path.dirname(__file__))
ia  = import_module("01_Inquiry")  # for Base, log_action
hb  = import_module("20_handoff")

#Base              = ia.Base
log_action        = ia.log_action
DepartmentType    = hb.DepartmentType
HandoffPackage    = hb.HandoffPackage
HandoffSummary    = hb.HandoffSummary


# ── HandoffRecord status ──────────────────────────────────────────────────

# class HandoffRecordStatus(str, Enum):
#     PENDING       = "pending"       # created, not yet sent
#     SENT          = "sent"          # notification dispatched
#     ACKNOWLEDGED  = "acknowledged"  # dept confirmed receipt
#     IN_PROGRESS   = "in_progress"   # dept started working
#     COMPLETED     = "completed"     # dept finished their task


# # ── DB model ──────────────────────────────────────────────────────────────

# class HandoffRecord(Base):
#     __tablename__ = "handoff_records"

#     id: Mapped[str]            = mapped_column(
#         String(36), primary_key=True, default=lambda: str(uuid.uuid4())
#     )
#     handoff_id: Mapped[str]    = mapped_column(String(36), index=True)
#     sales_order_id: Mapped[str] = mapped_column(String(36), index=True)
#     po_number: Mapped[str]     = mapped_column(String(100))
#     quotation_number: Mapped[str] = mapped_column(String(30))
#     buyer_company: Mapped[str] = mapped_column(String(255))

#     department: Mapped[str]    = mapped_column(String(30), index=True)
#     priority: Mapped[str]      = mapped_column(String(5))
#     job_reference: Mapped[str] = mapped_column(String(50))
#     subject: Mapped[str]       = mapped_column(String(255))

#     package_json: Mapped[str]  = mapped_column(Text)    # full HandoffPackage JSON
#     notification_channel: Mapped[str] = mapped_column(String(50), default="email")
#     notification_recipient: Mapped[str] = mapped_column(String(255), default="")

#     status: Mapped[str]        = mapped_column(String(30), default=HandoffRecordStatus.PENDING)
#     sent_at: Mapped[Optional[datetime]]         = mapped_column(DateTime, nullable=True)
#     acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
#     completed_at: Mapped[Optional[datetime]]    = mapped_column(DateTime, nullable=True)
#     acknowledged_by: Mapped[Optional[str]]      = mapped_column(String(100), nullable=True)

#     created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
#     updated_at: Mapped[datetime] = mapped_column(
#         DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
#     )


# ── Department → channel + recipient mapping ──────────────────────────────
# In prod: read from settings table / env vars

DEPT_CHANNELS: dict[str, dict] = {
    DepartmentType.PRODUCTION.value: {
        "channel":   "email",
        "recipient": "production@indussteel.in",
        "cc":        "production_head@indussteel.in",
    },
    DepartmentType.INVENTORY.value: {
        "channel":   "email",
        "recipient": "warehouse@indussteel.in",
        "cc":        "",
    },
    DepartmentType.PURCHASE.value: {
        "channel":   "email",
        "recipient": "purchase@indussteel.in",
        "cc":        "",
    },
    DepartmentType.DISPATCH.value: {
        "channel":   "email",
        "recipient": "dispatch@indussteel.in",
        "cc":        "logistics@indussteel.in",
    },
    DepartmentType.FINANCE.value: {
        "channel":   "email",
        "recipient": "accounts@indussteel.in",
        "cc":        "finance_head@indussteel.in",
    },
}


# ── Optional LLM cover note ───────────────────────────────────────────────

COVER_NOTE_PROMPT = """
Write a 3-sentence internal cover note for a B2B industrial sales handoff email.
It should be addressed to all departments and summarise the order being handed off.

Details:
  Customer     : {buyer}
  PO Number    : {po_no}
  Product      : {product}
  Quantity     : {qty} MT
  Total Value  : ₹{total:,.0f}
  Delivery By  : {deadline}

Tone: professional, internal memo style.
Include: what is being handed off, urgency level ({priority}), and request for prompt action.
"""


def generate_cover_note(
    summary: HandoffSummary,
    client: Optional[genai.Client],
) -> str:
    prod_pkg = next(
        (p for p in summary.packages if p.department == DepartmentType.PRODUCTION),
        summary.packages[0] if summary.packages else None
    )
    product  = prod_pkg.structured_data.get("product", "material") if prod_pkg else "material"
    qty      = prod_pkg.structured_data.get("production_qty", 0) if prod_pkg else 0
    deadline = prod_pkg.deadline if prod_pkg else "TBD"
    priority = prod_pkg.priority if prod_pkg else "P2"

    fallback = (
        f"INTERNAL HANDOFF MEMO\n"
        f"{'─'*50}\n"
        f"Customer     : {summary.buyer_company}\n"
        f"PO Number    : {summary.po_number}\n"
        f"Quotation    : {summary.quotation_number}\n"
        f"Total Value  : ₹{summary.total_value:,.0f}\n"
        f"Delivery By  : {deadline}\n"
        f"Priority     : {priority}\n"
        f"{'─'*50}\n"
        f"Please action your respective handoff packages immediately.\n"
        f"All departments must acknowledge receipt within 2 working hours.\n"
    )

    if not client:
        return fallback

    try:
        prompt = COVER_NOTE_PROMPT.format(
            buyer=summary.buyer_company,
            po_no=summary.po_number,
            product=product,
            qty=qty,
            total=summary.total_value,
            deadline=deadline,
            priority=priority,
        )
        resp = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
        return resp.text.strip()
    except Exception:
        return fallback


# ── Mock notifiers (swap real impl per channel) ───────────────────────────

def _mock_email(recipient: str, cc: str, subject: str, body: str) -> bool:
    print(f"  [EMAIL] → {recipient}" + (f" (cc: {cc})" if cc else ""))
    print(f"  Subject: {subject}")
    print(f"  Body   : {body[:120].replace(chr(10), ' ')}...")
    return True


def _mock_slack(channel: str, message: str) -> bool:
    print(f"  [SLACK #{channel}] {message[:100]}...")
    return True


def notify_department(
    package: HandoffPackage,
    cover_note: str,
    channel_config: dict,
) -> bool:
    """
    Mock notification dispatch. In prod:
      - email:   SendGrid / AWS SES
      - slack:   Slack SDK
      - webhook: POST to ERP endpoint
    """
    dept     = package.department.value
    channel  = channel_config.get("channel", "email")
    recipient = channel_config.get("recipient", "")
    cc       = channel_config.get("cc", "")

    body = (
        f"{cover_note}\n\n"
        f"{'='*50}\n"
        f"DEPARTMENT: {dept.upper()}\n"
        f"Job Ref   : {package.job_reference}\n"
        f"Priority  : {package.priority}\n"
        f"Subject   : {package.subject}\n\n"
        f"Summary:\n{package.summary}\n\n"
        f"Action Items:\n"
        + "\n".join(f"  {i+1}. {a}" for i, a in enumerate(package.action_items))
        + f"\n\nDeadline: {package.deadline or 'As per quotation'}"
    )

    if channel == "email":
        return _mock_email(recipient, cc, f"[{package.priority}] {package.subject}", body)
    elif channel == "slack":
        return _mock_slack(dept, f"*{package.subject}* | {package.priority} | {package.deadline}")

    print(f"  [{channel.upper()}] {dept} — {package.subject}")
    return True


def build_department_message(
    package: HandoffPackage,
    cover_note: str,
) -> str:
    department = package.department.value
    return (
        f"{cover_note}\n\n"
        f"{'='*50}\n"
        f"DEPARTMENT: {department.upper()}\n"
        f"Job Ref   : {package.job_reference}\n"
        f"Priority  : {package.priority}\n"
        f"Subject   : {package.subject}\n\n"
        f"Summary:\n{package.summary}\n\n"
        f"Action Items:\n"
        + "\n".join(
            f"  {index + 1}. {action}"
            for index, action in enumerate(package.action_items)
        )
        + (
            f"\n\nDeadline: "
            f"{package.deadline or 'As per quotation'}"
        )
    )


# ── DB operations ─────────────────────────────────────────────────────────

async def save_all_handoff_records(
    session: AsyncSession,
    summary: HandoffSummary,
    business_id: str,
    customer_id: Optional[str],
    thread_id: str,
) -> list[HandoffRecord]:
    records = []
    for pkg in summary.packages:
        cfg = DEPT_CHANNELS.get(pkg.department.value, {})
        record = HandoffRecord(
            business_id=business_id,
            customer_id=customer_id,
            thread_id=thread_id,
            handoff_id=summary.handoff_id,
            sales_order_id=summary.sales_order_id,
            po_number=summary.po_number,
            quotation_number=summary.quotation_number,
            buyer_company=summary.buyer_company,
            department=pkg.department.value,
            priority=pkg.priority,
            job_reference=pkg.job_reference,
            subject=pkg.subject,
            package_json=pkg.model_dump_json(),
            notification_channel=cfg.get("channel", "email"),
            notification_recipient=cfg.get("recipient", ""),
            status=HandoffRecordStatus.PENDING,
        )
        session.add(record)
        records.append(record)

    await session.flush()
    await log_action(
        session, "handoff", summary.handoff_id,
        "handoff_records_created", "handoff_agent",
        {
            "sales_order_id":   summary.sales_order_id,
            "po_number":        summary.po_number,
            "buyer":            summary.buyer_company,
            "total_value":      summary.total_value,
            "departments":      [p.department.value for p in summary.packages],
        },
    )
    await session.commit()
    return records


async def mark_sent(
    session: AsyncSession, record: HandoffRecord
) -> HandoffRecord:
    record.status  = HandoffRecordStatus.SENT
    record.sent_at = datetime.utcnow()
    await log_action(
        session, "handoff_record", record.id,
        "notification_sent", "handoff_agent",
        {"department": record.department, "recipient": record.notification_recipient},
    )
    return record


async def acknowledge_handoff(
    session: AsyncSession, record_id: str, acknowledged_by: str
) -> HandoffRecord:
    result = await session.execute(
        select(HandoffRecord).where(HandoffRecord.id == record_id)
    )
    record = result.scalar_one()
    record.status           = HandoffRecordStatus.ACKNOWLEDGED
    record.acknowledged_at  = datetime.utcnow()
    record.acknowledged_by  = acknowledged_by
    await log_action(
        session, "handoff_record", record_id,
        "handoff_acknowledged", acknowledged_by,
        {"department": record.department, "job_reference": record.job_reference},
    )
    await session.commit()
    return record


async def get_handoff_status(
    session: AsyncSession, sales_order_id: str
) -> list[dict]:
    result = await session.execute(
        select(HandoffRecord)
        .where(HandoffRecord.sales_order_id == sales_order_id)
        .order_by(HandoffRecord.department)
    )
    records = result.scalars().all()
    return [
        {
            "id":           r.id,
            "department":   r.department,
            "job_reference": r.job_reference,
            "priority":     r.priority,
            "status":       r.status,
            "sent_at":      r.sent_at.isoformat() if r.sent_at else None,
            "acknowledged_at": r.acknowledged_at.isoformat() if r.acknowledged_at else None,
            "acknowledged_by": r.acknowledged_by,
        }
        for r in records
    ]


# ── Master dispatch function ──────────────────────────────────────────────

async def dispatch_all(
    session: AsyncSession,
    summary: HandoffSummary,
    client: Optional[genai.Client] = None,
    business_id: str = "demo-steel-company",
    customer_id: Optional[str] = None,
    thread_id: str = "",
    outbound_dispatcher=None,
) -> list[HandoffRecord]:
    """
    1. Save all 5 department records to DB
    2. Generate cover note (LLM or fallback)
    3. Notify each department via their configured channel
    4. Mark each record as SENT
    5. Return all records
    """
    records = await save_all_handoff_records(
        session,
        summary,
        business_id=business_id,
        customer_id=customer_id,
        thread_id=thread_id or summary.sales_order_id,
    )
    cover_note = generate_cover_note(summary, client)

    print(f"\n{'='*60}")
    print(f"HANDOFF DISPATCH  —  {summary.handoff_id[:8]}")
    print(f"PO: {summary.po_number}  |  {summary.buyer_company}")
    # Keep console diagnostics encoding-safe on Windows terminals. Customer
    # and provider messages may still use the currency symbol.
    print(
        f"Total: INR {summary.total_value:,.0f}  |  "
        f"{len(summary.packages)} departments"
    )
    print(f"{'='*60}")

    for record, pkg in zip(records, summary.packages):
        cfg  = DEPT_CHANNELS.get(pkg.department.value, {})
        if outbound_dispatcher is None:
            continue
        result = await outbound_dispatcher.send(
            business_id=business_id,
            channel=cfg.get("channel", "email"),
            recipient=cfg.get("recipient", ""),
            subject=f"[{pkg.priority}] {pkg.subject}",
            text=build_department_message(pkg, cover_note),
        )
        if result.confirmed:
            await mark_sent(session, record)
            await log_action(
                session,
                "handoff_record",
                record.id,
                "provider_delivery_confirmed",
                "handoff_agent",
                {
                    "provider_message_id": (
                        result.provider_message_id
                    ),
                    "provider_status": result.status,
                },
            )

    await log_action(
        session, "handoff", summary.handoff_id,
        "all_departments_notified", "handoff_agent",
        {
            "departments_notified": [r.department for r in records],
            "sent_count": sum(1 for r in records if r.status == HandoffRecordStatus.SENT),
        },
    )
    await session.commit()
    return records


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    async def _demo():
        engine = create_async_engine("sqlite+aiosqlite:///sales_os.db")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
                 if os.environ.get("GEMINI_API_KEY") else None

        po_mod = import_module("18_PO")
        fe_mod = import_module("08_feasiblity")
        pe_mod = import_module("10_pricing_agent")

        po = po_mod.POExtraction(
            po_number="APX-PO-2025-0891",
            buyer_company="Apex Steel Pvt Ltd",
            buyer_gstin="03AABCA1234C1Z5",
            shipping_address="Apex Steel Works, Sahnewal, Ludhiana",
            product_description="MS Billet IS2062 100x100mm",
            quantity=500.0, unit="MT",
            price_per_unit_ex_gst=14000.0,
            gst_rate_pct=18.0, gst_amount=1_260_000,
            total_amount_inc_gst=8_260_000,
            payment_terms="20% advance, 80% net 45 days",
            delivery_date="30-06-2025", delivery_location="Ludhiana",
            special_conditions=["MTC required", "2 MT bundle packing"],
            extraction_confidence=0.97,
        )
        feasibility = fe_mod.FeasibilityResult(
            inquiry_id="INQ-001",
            fulfillment_type=fe_mod.FulfillmentType.PARTIAL_STOCK,
            stock_qty=150.0, production_qty=350.0,
            production_lead_days=14, transit_days=2,
            total_lead_time_days=16, customer_required_days=15,
            can_meet_deadline=True, delivery_location="Ludhiana",
            delivery_zone="North", location_found=True,
        )
        pricing = pe_mod.PricingResult(
            inquiry_id="INQ-001", product_code="MSB-001",
            product_name="MS Billet IS2062", quantity_mt=500.0,
            rm_cost_per_mt=11500, overhead_per_mt=920, transport_per_mt=450,
            total_cost_per_mt=12870, list_price_per_mt=14500,
            floor_price_per_mt=13989, suggested_price_per_mt=14500,
            customer_type="existing", applied_discount_pct=3.5,
            max_discount_pct=12.0, approval_limit_pct=8.0,
            discounted_price_per_mt=14000, min_margin_pct=8.0,
            target_margin_pct=15.0, actual_margin_pct=8.9,
            gst_rate_pct=18.0, gst_per_mt=2520,
            final_price_per_mt_ex_gst=14000, final_price_per_mt_inc_gst=16520,
            subtotal_ex_gst=7_000_000, gst_amount=1_260_000,
            total_invoice_value=8_260_000, pricing_possible=True,
        )
        qualification = import_module("06_customer").QualificationResult(
            inquiry_id="INQ-001", company_name="Apex Steel Pvt Ltd",
            customer_type="existing", score=82, score_breakdown={},
            temperature=import_module("06_customer").LeadTemperature.HOT,
            priority=import_module("06_customer").Priority.P1,
            rationale="Strong existing customer.",
        )

        summary = hb.build_all_packages(
            "SO-DEMO-001", po, feasibility, pricing,
            qualification, "QT-2025-A1B2",
        )

        async with Session() as session:
            records = await dispatch_all(session, summary, client)
            print(f"\n{'─'*60}")
            print("DISPATCH SUMMARY")
            status = await get_handoff_status(session, "SO-DEMO-001")
            for s in status:
                print(
                    f"  {s['department']:12} | {s['priority']} | "
                    f"{s['status']:15} | {s['job_reference']}"
                )

    asyncio.run(_demo())
