"""
Sub-problem: Revised Quotation + Version History

Responsibilities:
  1. QuotationVersion DB model — one row per revision
  2. build_revised_draft()  — clone original draft, apply new price
  3. save_quotation_version() — persist with version number + change reason
  4. get_version_history()   — return all versions for a quotation
  5. render_version_diff()   — plain-English summary of what changed

Version numbering:
  V1 = original quotation (auto-created when first quotation is saved)
  V2 = first revised quotation after negotiation
  V3 = second revision, and so on

Design rule: NO LLM in this file. Every operation is data transformation.

Run:
    python 17_revised_quotation.py
"""

import uuid
import json
import copy
import sys
import os
import asyncio
from datetime import datetime
from typing import Optional
from importlib import import_module

from pydantic import BaseModel
from sqlalchemy import String, Text, Numeric, Integer, DateTime, ForeignKey, select, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import (
    create_async_engine, async_sessionmaker, AsyncSession,
)

from app.database.base import Base

from app.database.models.quotation import (
    QuotationStatus,
    QuotationVersion,
)

sys.path.insert(0, os.path.dirname(__file__))
ia  = import_module("01_Inquiry")
qb  = import_module("11_quotation")
qr  = import_module("12_quotation")
ne  = import_module("16_negotion")

#Base              = ia.Base
log_action        = ia.log_action
QuotationDraft    = qb.QuotationDraft
QuotationLineItem = qb.QuotationLineItem
#QuotationStatus   = qb.QuotationStatus
render_quotation_html = qr.render_quotation_html
NegotiationDecision = ne.NegotiationDecision
NegotiationAnalysis = ne.NegotiationAnalysis


# ── QuotationVersion DB model ─────────────────────────────────────────────

# class QuotationVersion(Base):
#     __tablename__ = "quotation_versions"

#     id: Mapped[str]              = mapped_column(String(36), primary_key=True,
#                                                   default=lambda: str(uuid.uuid4()))
#     quotation_id: Mapped[str]    = mapped_column(String(36), index=True)
#     quotation_number: Mapped[str] = mapped_column(String(30), index=True)
#     version_number: Mapped[int]  = mapped_column(Integer)     # 1 = original, 2+ = revisions

#     # Pricing snapshot for this version
#     price_per_mt_ex_gst: Mapped[float]
#     discount_pct: Mapped[float]
#     subtotal_ex_gst: Mapped[float]
#     gst_amount: Mapped[float]
#     total_inc_gst: Mapped[float]

#     # Audit
#     change_reason: Mapped[str]  = mapped_column(String(255))
#     # "initial" | "customer_counteroffer" | "manager_approved" | "manager_override"
#     changed_by: Mapped[str]     = mapped_column(String(100))  # "auto" | manager name
#     negotiation_decision: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
#     customer_offered_price: Mapped[Optional[float]] = mapped_column(nullable=True)

#     draft_json: Mapped[str]     = mapped_column(Text)          # full QuotationDraft JSON
#     html_content: Mapped[str]   = mapped_column(Text)          # rendered HTML for this version

#     created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Pydantic snapshot for passing between agents ──────────────────────────

class VersionSummary(BaseModel):
    version_number: int
    price_per_mt_ex_gst: float
    discount_pct: float
    total_inc_gst: float
    change_reason: str
    changed_by: str
    negotiation_decision: Optional[str]
    customer_offered_price: Optional[float]
    created_at: str


# ── Build a revised QuotationDraft at a new price ─────────────────────────

def build_revised_draft(
    original_draft: QuotationDraft,
    analysis: NegotiationAnalysis,
    version_number: int,
    changed_by: str = "auto",
) -> QuotationDraft:
    """
    Deep-clones the original draft, updates:
      - line item prices
      - subtotal / GST / total
      - status (back to DRAFT for re-approval)
      - notes any revision in payment terms if needed
    Returns a new QuotationDraft — original is never mutated.
    """
    new_price  = analysis.customer_price_per_mt
    gst_rate   = original_draft.line_items[0].gst_rate_pct if original_draft.line_items else 18.0

    # Recompute line items
    new_items: list[QuotationLineItem] = []
    for li in original_draft.line_items:
        gst_per_unit = round(new_price * gst_rate / 100, 2)
        total_inc    = round((new_price + gst_per_unit) * li.quantity, 2)
        new_li = QuotationLineItem(
            sr_no=li.sr_no,
            product_code=li.product_code,
            description=li.description,
            specification=li.specification,
            quantity=li.quantity,
            unit=li.unit,
            unit_price_ex_gst=analysis.original_price_per_mt,   # original list price
            discount_pct=analysis.implied_discount_pct,
            discounted_price_ex_gst=new_price,
            gst_rate_pct=gst_rate,
            gst_amount_per_unit=gst_per_unit,
            total_inc_gst=total_inc,
        )
        new_items.append(new_li)

    # New totals
    new_subtotal = analysis.revised_subtotal_ex_gst
    new_gst      = analysis.revised_gst_amount
    new_total    = analysis.revised_total_inc_gst

    # Build revised draft as a copy with new number + status
    revision_suffix = f"-R{version_number}"
    new_qt_number   = original_draft.quotation_number + revision_suffix

    return QuotationDraft(
        quotation_number=new_qt_number,
        inquiry_id=original_draft.inquiry_id,
        quotation_date=datetime.now().strftime("%d-%m-%Y"),
        valid_until=original_draft.valid_until,

        seller_name=original_draft.seller_name,
        seller_address=original_draft.seller_address,
        seller_gstin=original_draft.seller_gstin,
        seller_email=original_draft.seller_email,
        seller_phone=original_draft.seller_phone,
        seller_bank=original_draft.seller_bank,

        buyer_company=original_draft.buyer_company,
        buyer_contact=original_draft.buyer_contact,
        buyer_delivery_location=original_draft.buyer_delivery_location,
        buyer_gstin=original_draft.buyer_gstin,

        line_items=new_items,
        subtotal_ex_gst=new_subtotal,
        total_gst_amount=new_gst,
        total_inc_gst=new_total,

        payment_terms_code=original_draft.payment_terms_code,
        payment_terms_text=original_draft.payment_terms_text,
        delivery_timeline=original_draft.delivery_timeline,
        freight_terms=original_draft.freight_terms,
        warranty=original_draft.warranty,
        terms_and_conditions=original_draft.terms_and_conditions,

        # Revised quotation is draft until reviewed
        status=QuotationStatus.APPROVED if analysis.can_auto_approve else QuotationStatus.PENDING_APPROVAL,
        requires_human_approval=not analysis.can_auto_approve,
        approval_reasons=(
            [] if analysis.can_auto_approve
            else [analysis.human_approval_reason or "Discount requires approval"]
        ),
        prepared_by=changed_by,
    )


# ── Persist version to DB ─────────────────────────────────────────────────

async def get_next_version_number(
    session: AsyncSession, quotation_id: str
) -> int:
    result = await session.execute(
        select(func.max(QuotationVersion.version_number))
        .where(QuotationVersion.quotation_id == quotation_id)
    )
    current_max = result.scalar_one_or_none()
    return (current_max or 0) + 1


async def save_quotation_version(
    session: AsyncSession,
    quotation_id: str,
    quotation_number: str,
    draft: QuotationDraft,
    analysis: Optional[NegotiationAnalysis],
    change_reason: str,
    changed_by: str = "auto",
    business_id: str = "demo-steel-company",
    customer_id: Optional[str] = None,
    thread_id: str = "",
) -> QuotationVersion:

    version_num = await get_next_version_number(session, quotation_id)
    html        = render_quotation_html(draft)

    price_per_mt = (
        draft.line_items[0].discounted_price_ex_gst
        if draft.line_items else 0.0
    )
    discount_pct = (
        draft.line_items[0].discount_pct
        if draft.line_items else 0.0
    )

    record = QuotationVersion(
        business_id=business_id,
        customer_id=customer_id,
        thread_id=thread_id or quotation_id,
        quotation_id=quotation_id,
        quotation_number=quotation_number,
        version_number=version_num,
        price_per_mt_ex_gst=price_per_mt,
        discount_pct=discount_pct,
        subtotal_ex_gst=draft.subtotal_ex_gst,
        gst_amount=draft.total_gst_amount,
        total_inc_gst=draft.total_inc_gst,
        change_reason=change_reason,
        changed_by=changed_by,
        negotiation_decision=analysis.decision.value if analysis else None,
        customer_offered_price=analysis.customer_price_per_mt if analysis else None,
        draft_json=draft.model_dump_json(),
        html_content=html,
    )
    session.add(record)
    await session.flush()
    await log_action(
        session, "quotation_version", record.id,
        "version_created", changed_by,
        {
            "quotation_number": quotation_number,
            "version": version_num,
            "price_per_mt": price_per_mt,
            "discount_pct": discount_pct,
            "total": draft.total_inc_gst,
            "decision": analysis.decision.value if analysis else "initial",
            "change_reason": change_reason,
        },
    )
    await session.commit()
    return record


async def get_version_history(
    session: AsyncSession, quotation_id: str
) -> list[VersionSummary]:
    result = await session.execute(
        select(QuotationVersion)
        .where(QuotationVersion.quotation_id == quotation_id)
        .order_by(QuotationVersion.version_number)
    )
    rows = result.scalars().all()
    return [
        VersionSummary(
            version_number=r.version_number,
            price_per_mt_ex_gst=r.price_per_mt_ex_gst,
            discount_pct=r.discount_pct,
            total_inc_gst=r.total_inc_gst,
            change_reason=r.change_reason,
            changed_by=r.changed_by,
            negotiation_decision=r.negotiation_decision,
            customer_offered_price=r.customer_offered_price,
            created_at=r.created_at.isoformat(),
        )
        for r in rows
    ]


# ── Plain-English diff between two versions ───────────────────────────────

def render_version_diff(v1: VersionSummary, v2: VersionSummary) -> str:
    price_delta   = v2.price_per_mt_ex_gst - v1.price_per_mt_ex_gst
    total_delta   = v2.total_inc_gst - v1.total_inc_gst
    disc_delta    = v2.discount_pct - v1.discount_pct

    lines = [
        f"V{v1.version_number} → V{v2.version_number}  |  {v2.change_reason}",
        f"  Price/MT : ₹{v1.price_per_mt_ex_gst:,.0f} → ₹{v2.price_per_mt_ex_gst:,.0f}"
        f"  ({price_delta:+,.0f}/MT)",
        f"  Discount : {v1.discount_pct:.1f}% → {v2.discount_pct:.1f}%"
        f"  ({disc_delta:+.1f}%)",
        f"  Total    : ₹{v1.total_inc_gst:,.0f} → ₹{v2.total_inc_gst:,.0f}"
        f"  ({total_delta:+,.0f})",
        f"  Decision : {v2.negotiation_decision or 'N/A'}"
        + (f"  (customer offered ₹{v2.customer_offered_price:,.0f}/MT)"
           if v2.customer_offered_price else ""),
    ]
    return "\n".join(lines)


# ── Demo ──────────────────────────────────────────────────────────────────

async def _demo():
    engine = create_async_engine("sqlite+aiosqlite:///sales_os.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    pe_mod = import_module("10_pricing_agent")

    # Build a sample original draft
    original_draft = QuotationDraft(
        quotation_number="QT-2025-A1B2",
        inquiry_id="INQ-001",
        valid_until="30-07-2025",
        buyer_company="Apex Steel Pvt Ltd",
        buyer_contact="Ramesh Kumar",
        buyer_delivery_location="Ludhiana",
        seller_name="IndusSteel Trading Pvt. Ltd.",
        seller_email="sales@indussteel.in",
        subtotal_ex_gst=7_100_000,
        total_gst_amount=1_278_000,
        total_inc_gst=8_378_000,
        payment_terms_text="20% advance, balance net 45 days",
        delivery_timeline="Ex-stock within 2-3 days.",
        line_items=[
            QuotationLineItem(
                sr_no=1, product_code="MSB-001",
                description="MS Billet IS2062",
                specification="100x100mm square section",
                quantity=500.0, unit="MT",
                unit_price_ex_gst=14500.0, discount_pct=2.1,
                discounted_price_ex_gst=14200.0,
                gst_rate_pct=18.0, gst_amount_per_unit=2556.0,
                total_inc_gst=8_378_000.0,
            )
        ],
    )

    pricing = pe_mod.PricingResult(
        inquiry_id="INQ-001", product_code="MSB-001", product_name="MS Billet IS2062",
        quantity_mt=500.0, rm_cost_per_mt=11500, overhead_per_mt=920,
        transport_per_mt=450, total_cost_per_mt=12870, list_price_per_mt=14500,
        floor_price_per_mt=13989, suggested_price_per_mt=14500,
        customer_type="existing", applied_discount_pct=2.1, max_discount_pct=12.0,
        approval_limit_pct=8.0, discounted_price_per_mt=14200,
        min_margin_pct=8.0, target_margin_pct=15.0, actual_margin_pct=10.5,
        gst_rate_pct=18.0, gst_per_mt=2556, final_price_per_mt_ex_gst=14200,
        final_price_per_mt_inc_gst=16756, subtotal_ex_gst=7_100_000,
        gst_amount=1_278_000, total_invoice_value=8_378_000, pricing_possible=True,
    )

    QUOTATION_ID = "Q-DEMO-001"

    async with Session() as session:
        # V1 — original
        v1_rec = await save_quotation_version(
            session, QUOTATION_ID, "QT-2025-A1B2",
            original_draft, analysis=None,
            change_reason="initial", changed_by="sales_agent",
        )
        print(f"Saved V{v1_rec.version_number}: original  ₹{v1_rec.price_per_mt_ex_gst:,.0f}/MT")

        # Customer counter-offers ₹14,000/MT → ACCEPTABLE
        analysis_1 = ne.evaluate_counteroffer(14000.0, pricing)
        revised_1  = build_revised_draft(original_draft, analysis_1, version_number=2)
        v2_rec = await save_quotation_version(
            session, QUOTATION_ID, "QT-2025-A1B2",
            revised_1, analysis_1,
            change_reason="customer_counteroffer",
            changed_by="auto",
        )
        print(f"Saved V{v2_rec.version_number}: revised   ₹{v2_rec.price_per_mt_ex_gst:,.0f}/MT"
              f"  decision={v2_rec.negotiation_decision}")

        # Customer counters again ₹13,600/MT → BELOW_FLOOR → manager override at floor
        analysis_2 = ne.evaluate_counteroffer(13989.0, pricing)  # floor price as counter
        revised_2  = build_revised_draft(original_draft, analysis_2, version_number=3)
        v3_rec = await save_quotation_version(
            session, QUOTATION_ID, "QT-2025-A1B2",
            revised_2, analysis_2,
            change_reason="manager_approved_floor_price",
            changed_by="sales_manager",
        )
        print(f"Saved V{v3_rec.version_number}: floor     ₹{v3_rec.price_per_mt_ex_gst:,.0f}/MT"
              f"  decision={v3_rec.negotiation_decision}")

        # Show version history
        history = await get_version_history(session, QUOTATION_ID)
        print(f"\n{'='*60}")
        print(f"VERSION HISTORY  —  Quotation QT-2025-A1B2")
        print(f"{'='*60}")
        for v in history:
            cust = f"  ← customer offered ₹{v.customer_offered_price:,.0f}/MT" \
                   if v.customer_offered_price else ""
            print(f"  V{v.version_number}  ₹{v.price_per_mt_ex_gst:,.0f}/MT  "
                  f"disc={v.discount_pct:.1f}%  "
                  f"total=₹{v.total_inc_gst:,.0f}  "
                  f"by={v.changed_by}{cust}")

        # Diffs
        print(f"\n{'='*60}")
        print("CHANGE DIFFS")
        print(f"{'='*60}")
        for i in range(len(history) - 1):
            print(render_version_diff(history[i], history[i+1]))
            print()


if __name__ == "__main__":
    asyncio.run(_demo())
