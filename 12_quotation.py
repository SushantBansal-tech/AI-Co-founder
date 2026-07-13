"""
Sub-problem: Quotation Renderer + Dispatcher

Responsibilities:
  1. Render QuotationDraft → professional HTML document
  2. Persist quotation to DB (Quotation table)
  3. Human validation gate — sets PENDING_APPROVAL, waits for sign-off
  4. approve_quotation() / reject_quotation() — human actions
  5. dispatch_quotation() — send via email or WhatsApp (mock; swap real SDK in prod)
  6. Full audit log on every state change

Depends on:
  inquiry_agent.py         → Base, AuditLog, log_action
  11_quotation_builder.py  → QuotationDraft, QuotationStatus

Design:
  - HTML is built programmatically (no Jinja2 needed)
  - Dispatch is mocked — real impl swaps in SendGrid / WhatsApp Business API
  - Every state change (draft → pending → approved → sent) is audit-logged

Run:
    python 12_quotation_renderer.py
    Outputs: quotation_INQ-001.html  (open in browser to see full document)
"""

import os
import sys
import uuid
import asyncio
from datetime import datetime
from typing import Optional
from importlib import import_module

from pydantic import BaseModel
from sqlalchemy import String, Text, DateTime, Enum as SAEnum, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

sys.path.insert(0, os.path.dirname(__file__))
ia    = import_module("01_Inquiry")  # for Base, log_action, InquiryExtraction
qb    = import_module("11_quotation")

Base            = ia.Base
log_action      = ia.log_action
QuotationDraft  = qb.QuotationDraft
QuotationStatus = qb.QuotationStatus
QuotationLineItem = qb.QuotationLineItem


# -----------------------------------------------------------------------
# DB model
# -----------------------------------------------------------------------

class QuotationRecord(Base):
    __tablename__ = "quotations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True,
                                     default=lambda: str(uuid.uuid4()))
    quotation_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    inquiry_id: Mapped[str]       = mapped_column(String(36), index=True)
    status: Mapped[str]           = mapped_column(SAEnum(QuotationStatus))
    buyer_company: Mapped[str]    = mapped_column(String(255))
    total_inc_gst: Mapped[float]  = mapped_column(default=0.0)
    requires_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    draft_json: Mapped[str]       = mapped_column(Text)   # full QuotationDraft JSON
    html_content: Mapped[str]     = mapped_column(Text)   # rendered HTML
    approved_by: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    sent_via: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    sent_to: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime]  = mapped_column(DateTime, default=datetime.utcnow,
                                                   onupdate=datetime.utcnow)


# -----------------------------------------------------------------------
# Dispatch result
# -----------------------------------------------------------------------

class DispatchResult(BaseModel):
    quotation_number: str
    channel: str          # "email" | "whatsapp"
    recipient: str
    success: bool
    message: str
    dispatched_at: str


# -----------------------------------------------------------------------
# HTML renderer — builds a professional B2B quotation document
# -----------------------------------------------------------------------

def _css() -> str:
    return """
    <style>
      * { box-sizing: border-box; margin: 0; padding: 0; }
      body { font-family: Arial, sans-serif; font-size: 12px;
             color: #2c2c2c; padding: 30px; background: #fff; }
      /* Header */
      .header { display: flex; justify-content: space-between;
                align-items: flex-start; border-bottom: 3px solid #1a3c6e;
                padding-bottom: 14px; margin-bottom: 18px; }
      .company-block h1 { font-size: 20px; color: #1a3c6e; }
      .company-block p { font-size: 11px; color: #555; line-height: 1.6; }
      .quotation-badge { text-align: right; }
      .quotation-badge h2 { font-size: 26px; color: #1a3c6e;
                            letter-spacing: 2px; }
      .quotation-badge p { font-size: 11px; color: #555; }
      /* Approval banner */
      .approval-banner { background: #fff8e1; border-left: 4px solid #f59e0b;
                         padding: 10px 14px; margin-bottom: 14px;
                         border-radius: 4px; }
      .approval-banner strong { color: #b45309; }
      /* Meta grid */
      .meta-grid { display: grid; grid-template-columns: 1fr 1fr;
                   gap: 14px; margin-bottom: 18px; }
      .meta-box { border: 1px solid #d1d9e6; border-radius: 6px; padding: 10px; }
      .meta-box h4 { font-size: 10px; text-transform: uppercase;
                     color: #1a3c6e; letter-spacing: 1px;
                     margin-bottom: 6px; border-bottom: 1px solid #d1d9e6;
                     padding-bottom: 4px; }
      .meta-box p { line-height: 1.7; color: #333; }
      /* Line items table */
      table.items { width: 100%; border-collapse: collapse; margin: 18px 0; }
      table.items thead tr { background: #1a3c6e; color: white; }
      table.items th { padding: 8px 6px; text-align: left;
                       font-size: 10px; letter-spacing: 0.5px; }
      table.items td { padding: 8px 6px; border-bottom: 1px solid #e5e9f0; }
      table.items tbody tr:nth-child(even) { background: #f7f9fc; }
      .text-right { text-align: right; }
      /* Totals */
      .totals-table { width: 320px; margin-left: auto;
                      border-collapse: collapse; margin-top: 6px; }
      .totals-table td { padding: 5px 8px; }
      .totals-table .subtotal td { color: #444; }
      .totals-table .gst-row td { color: #444; }
      .totals-table .grand-total td { background: #1a3c6e; color: white;
                                       font-weight: bold; font-size: 13px;
                                       padding: 8px; }
      /* Terms sections */
      .terms-grid { display: grid; grid-template-columns: 1fr 1fr;
                    gap: 14px; margin-top: 18px; }
      .terms-box { border: 1px solid #d1d9e6; border-radius: 6px; padding: 10px; }
      .terms-box h4 { font-size: 10px; text-transform: uppercase;
                      color: #1a3c6e; letter-spacing: 1px;
                      margin-bottom: 8px; }
      .terms-box p, .terms-box li { line-height: 1.7; color: #444; }
      .terms-box ol { padding-left: 16px; }
      .terms-box li { margin-bottom: 4px; font-size: 11px; }
      /* Footer */
      .footer { border-top: 2px solid #1a3c6e; margin-top: 24px;
                padding-top: 12px; display: flex;
                justify-content: space-between; align-items: flex-end; }
      .signature-block { text-align: right; }
      .signature-line { border-top: 1px solid #333; width: 180px;
                        margin-top: 40px; padding-top: 4px;
                        font-size: 11px; color: #555; }
    </style>
    """


def _approval_banner(draft: QuotationDraft) -> str:
    if not draft.requires_human_approval:
        return ""
    reasons_html = "".join(f"<li>{r}</li>" for r in draft.approval_reasons)
    return f"""
    <div class="approval-banner">
      <strong>⚠ PENDING HUMAN APPROVAL</strong><br>
      This quotation cannot be sent until approved. Reasons:<ul>{reasons_html}</ul>
    </div>"""


def _line_items_rows(items: list[QuotationLineItem]) -> str:
    rows = ""
    for li in items:
        rows += f"""
        <tr>
          <td>{li.sr_no}</td>
          <td><strong>{li.description}</strong><br>
              <span style="color:#666;font-size:11px">{li.product_code}</span></td>
          <td>{li.specification}</td>
          <td class="text-right">{li.quantity:.2f}</td>
          <td>{li.unit}</td>
          <td class="text-right">₹{li.unit_price_ex_gst:,.2f}</td>
          <td class="text-right">{li.discount_pct:.1f}%</td>
          <td class="text-right">₹{li.discounted_price_ex_gst:,.2f}</td>
          <td class="text-right">{li.gst_rate_pct:.0f}%</td>
          <td class="text-right">₹{li.gst_amount_per_unit * li.quantity:,.2f}</td>
          <td class="text-right"><strong>₹{li.total_inc_gst:,.2f}</strong></td>
        </tr>"""
    return rows


def _tnc_items(clauses: list[str]) -> str:
    return "".join(f"<li>{c}</li>" for c in clauses)


def render_quotation_html(draft: QuotationDraft) -> str:
    """Builds a complete, self-contained HTML quotation document."""

    status_badge_color = {
        QuotationStatus.DRAFT: "#6b7280",
        QuotationStatus.PENDING_APPROVAL: "#d97706",
        QuotationStatus.APPROVED: "#059669",
        QuotationStatus.SENT: "#2563eb",
        QuotationStatus.REJECTED: "#dc2626",
    }.get(draft.status, "#6b7280")

    gstin_line = f"<br>GSTIN: {draft.buyer_gstin}" if draft.buyer_gstin else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Quotation {draft.quotation_number}</title>
  {_css()}
</head>
<body>

  <!-- ── HEADER ─────────────────────────────────────────── -->
  <div class="header">
    <div class="company-block">
      <h1>{draft.seller_name}</h1>
      <p>{draft.seller_address}<br>
         GSTIN: {draft.seller_gstin}<br>
         {draft.seller_email} | {draft.seller_phone}</p>
    </div>
    <div class="quotation-badge">
      <h2>QUOTATION</h2>
      <p style="margin-top:6px">
        <strong>No.:</strong> {draft.quotation_number}<br>
        <strong>Date:</strong> {draft.quotation_date}<br>
        <strong>Valid Until:</strong> {draft.valid_until}<br>
        <span style="background:{status_badge_color};color:white;
                     padding:2px 8px;border-radius:3px;font-size:10px">
          {draft.status.value.upper().replace("_"," ")}
        </span>
      </p>
    </div>
  </div>

  {_approval_banner(draft)}

  <!-- ── META GRID ──────────────────────────────────────── -->
  <div class="meta-grid">
    <div class="meta-box">
      <h4>Bill To / Ship To</h4>
      <p><strong>{draft.buyer_company}</strong><br>
         {draft.buyer_contact or ''}{gstin_line}<br>
         Delivery: {draft.buyer_delivery_location}</p>
    </div>
    <div class="meta-box">
      <h4>Reference</h4>
      <p>Inquiry ID: {draft.inquiry_id}<br>
         Prepared by: {draft.prepared_by}<br>
         Fulfillment: {draft.fulfillment_type.replace("_"," ").title()}</p>
    </div>
  </div>

  <!-- ── LINE ITEMS ─────────────────────────────────────── -->
  <table class="items">
    <thead>
      <tr>
        <th>#</th>
        <th>Description</th>
        <th>Specification</th>
        <th class="text-right">Qty</th>
        <th>Unit</th>
        <th class="text-right">Unit Price</th>
        <th class="text-right">Disc%</th>
        <th class="text-right">Net Price</th>
        <th class="text-right">GST%</th>
        <th class="text-right">GST Amt</th>
        <th class="text-right">Total</th>
      </tr>
    </thead>
    <tbody>{_line_items_rows(draft.line_items)}</tbody>
  </table>

  <!-- ── TOTALS ─────────────────────────────────────────── -->
  <table class="totals-table">
    <tr class="subtotal">
      <td>Subtotal (ex-GST)</td>
      <td class="text-right">₹{draft.subtotal_ex_gst:,.2f}</td>
    </tr>
    <tr class="gst-row">
      <td>GST Amount</td>
      <td class="text-right">₹{draft.total_gst_amount:,.2f}</td>
    </tr>
    <tr class="grand-total">
      <td>TOTAL INVOICE VALUE</td>
      <td class="text-right">₹{draft.total_inc_gst:,.2f}</td>
    </tr>
  </table>

  <!-- ── COMMERCIAL TERMS ───────────────────────────────── -->
  <div class="terms-grid">
    <div class="terms-box">
      <h4>Payment Terms</h4>
      <p>{draft.payment_terms_text}</p>
    </div>
    <div class="terms-box">
      <h4>Delivery &amp; Dispatch</h4>
      <p>{draft.delivery_timeline}</p>
    </div>
    <div class="terms-box">
      <h4>Freight Terms</h4>
      <p>{draft.freight_terms}</p>
    </div>
    <div class="terms-box">
      <h4>Warranty</h4>
      <p>{draft.warranty}</p>
    </div>
  </div>

  <!-- ── T&C ────────────────────────────────────────────── -->
  <div class="terms-box" style="margin-top:14px">
    <h4>Terms &amp; Conditions</h4>
    <ol>{_tnc_items(draft.terms_and_conditions)}</ol>
  </div>

  <!-- ── BANK DETAILS ───────────────────────────────────── -->
  <div class="terms-box" style="margin-top:14px">
    <h4>Bank Details (for RTGS/NEFT)</h4>
    <p>{draft.seller_bank}</p>
  </div>

  <!-- ── FOOTER ─────────────────────────────────────────── -->
  <div class="footer">
    <div>
      <p style="font-size:11px;color:#888">
        This is a computer-generated quotation. Subject to terms stated above.
      </p>
    </div>
    <div class="signature-block">
      <div class="signature-line">
        For {draft.seller_name}<br>Authorised Signatory
      </div>
    </div>
  </div>

</body>
</html>"""
    return html


# -----------------------------------------------------------------------
# DB persistence
# -----------------------------------------------------------------------

async def save_quotation(
    session: AsyncSession,
    draft: QuotationDraft,
    html: str,
) -> QuotationRecord:
    record = QuotationRecord(
        quotation_number=draft.quotation_number,
        inquiry_id=draft.inquiry_id,
        status=draft.status,
        buyer_company=draft.buyer_company,
        total_inc_gst=draft.total_inc_gst,
        requires_approval=draft.requires_human_approval,
        draft_json=draft.model_dump_json(),
        html_content=html,
    )
    session.add(record)
    await session.flush()
    await log_action(
        session, "quotation", record.id,
        "quotation_created", "quotation_agent",
        {"quotation_number": draft.quotation_number,
         "status": draft.status.value,
         "total": draft.total_inc_gst,
         "requires_approval": draft.requires_human_approval},
    )
    await session.commit()
    return record


# -----------------------------------------------------------------------
# Human validation gate
# -----------------------------------------------------------------------

async def approve_quotation(
    session: AsyncSession,
    record: QuotationRecord,
    approved_by: str,
) -> QuotationRecord:
    record.status     = QuotationStatus.APPROVED
    record.approved_by = approved_by
    record.updated_at  = datetime.utcnow()
    await log_action(
        session, "quotation", record.id,
        "quotation_approved", approved_by,
        {"quotation_number": record.quotation_number},
    )
    await session.commit()
    return record


async def reject_quotation(
    session: AsyncSession,
    record: QuotationRecord,
    rejected_by: str,
    reason: str,
) -> QuotationRecord:
    record.status           = QuotationStatus.REJECTED
    record.rejection_reason = reason
    record.updated_at       = datetime.utcnow()
    await log_action(
        session, "quotation", record.id,
        "quotation_rejected", rejected_by,
        {"quotation_number": record.quotation_number, "reason": reason},
    )
    await session.commit()
    return record


# -----------------------------------------------------------------------
# Dispatch — mock implementation (real: swap SendGrid / WhatsApp Business)
# -----------------------------------------------------------------------

def _send_email(recipient: str, subject: str, html_body: str) -> bool:
    """
    Mock email send.
    Real impl: SendGrid / AWS SES / Mailgun
      sendgrid.send(to=recipient, subject=subject, html=html_body)
    """
    print(f"  [MOCK EMAIL] → {recipient}")
    print(f"  Subject : {subject}")
    print(f"  Body    : {len(html_body)} chars of HTML")
    return True


def _send_whatsapp(phone: str, message: str) -> bool:
    """
    Mock WhatsApp send.
    Real impl: WhatsApp Business Cloud API
      requests.post(META_API_URL, json={to: phone, text: message})
    """
    print(f"  [MOCK WHATSAPP] → {phone}")
    print(f"  Message : {message[:120]}...")
    return True


async def dispatch_quotation(
    session: AsyncSession,
    record: QuotationRecord,
    draft: QuotationDraft,
    channel: str,             # "email" | "whatsapp"
    recipient: str,           # email address or phone number
) -> DispatchResult:
    """
    Sends the quotation to the customer.
    Blocks if status is not APPROVED (or DRAFT for auto-approved quotations).
    """
    if record.status not in (QuotationStatus.APPROVED, QuotationStatus.DRAFT):
        return DispatchResult(
            quotation_number=draft.quotation_number,
            channel=channel, recipient=recipient,
            success=False,
            message=f"Cannot dispatch — quotation status is {record.status.value}.",
            dispatched_at=datetime.utcnow().isoformat(),
        )

    success = False
    if channel == "email":
        subject = (f"Quotation {draft.quotation_number} from {draft.seller_name} "
                   f"| {draft.buyer_company}")
        success = _send_email(recipient, subject, record.html_content)
    elif channel == "whatsapp":
        msg = (
            f"Dear {draft.buyer_contact or 'Sir/Madam'},\n\n"
            f"Please find our quotation {draft.quotation_number} for "
            f"{draft.line_items[0].description if draft.line_items else 'your requirement'}.\n\n"
            f"Total Value: ₹{draft.total_inc_gst:,.0f} (incl. GST)\n"
            f"Valid Until: {draft.valid_until}\n"
            f"Payment: {draft.payment_terms_text}\n\n"
            f"Full quotation sent to your registered email. "
            f"Regards, {draft.seller_name}"
        )
        success = _send_whatsapp(recipient, msg)

    if success:
        record.status   = QuotationStatus.SENT
        record.sent_via = channel
        record.sent_to  = recipient
        record.updated_at = datetime.utcnow()
        await log_action(
            session, "quotation", record.id,
            "quotation_sent", "dispatch_agent",
            {"channel": channel, "recipient": recipient,
             "quotation_number": draft.quotation_number},
        )
        await session.commit()

    return DispatchResult(
        quotation_number=draft.quotation_number,
        channel=channel, recipient=recipient,
        success=success,
        message="Dispatched successfully." if success else "Dispatch failed.",
        dispatched_at=datetime.utcnow().isoformat(),
    )


# -----------------------------------------------------------------------
# Demo — full render + DB + dispatch simulation
# -----------------------------------------------------------------------

async def _demo():
    # Re-use the draft from 11's demo
    qb_mod   = import_module("11_quotation")
    cat_mod  = import_module("03_catalog")
    req_mod  = import_module("04_requirment")
    pd_mod   = import_module("09_pricing")
    lk_mod   = import_module("05_customer_qual")
    ia_mod   = import_module("01_Inquiry")
    fe_mod   = import_module("08_feasiblity")
    qual_mod = import_module("06_customer")
    pe_mod   = import_module("10_pricing_agent")

    MatchType       = req_mod.MatchType
    FulfillmentType = fe_mod.FulfillmentType
    Priority        = qual_mod.Priority
    LeadTemperature = qual_mod.LeadTemperature
    PaymentBehavior = lk_mod.PaymentBehavior
    CustomerType    = lk_mod.CustomerType

    extraction = ia_mod.InquiryExtraction(
        inquiry_id="INQ-001", customer_name="Ramesh Kumar",
        company_name="Apex Steel Pvt Ltd", contact_person="Ramesh Kumar",
        product_requested="MS Billet IS2062", quantity="500 MT",
        specifications="100x100mm square section",
        delivery_location="Ludhiana", delivery_date="within 30 days",
        extraction_confidence=0.95,
    )
    product = cat_mod.CatalogProduct(
        product_code="MSB-001", name="MS Billet",
        category="Steel Billet", unit="MT",
    )
    req_summary = req_mod.RequirementSummary(
        inquiry_id="INQ-001", match_type=MatchType.EXACT,
        matched_product=product, similarity_score=0.92,
        summary_text="Exact match.",
    )
    customer = lk_mod.CustomerProfile(
        customer_type=CustomerType.EXISTING,
        company_name="Apex Steel Pvt Ltd",
        contact_person="Ramesh Kumar",
        gstin="03AABCA1234C1Z5",
        city="Ludhiana",
        payment_behavior=PaymentBehavior.GOOD,
        credit_limit=5_000_000, outstanding_amount=1_200_000,
        credit_utilization_pct=24.0,
    )
    qualification = qual_mod.QualificationResult(
        inquiry_id="INQ-001", company_name="Apex Steel Pvt Ltd",
        customer_type="existing", score=82, score_breakdown={},
        temperature=LeadTemperature.HOT, priority=Priority.P1,
        rationale="Strong existing customer.",
    )
    feasibility = fe_mod.FeasibilityResult(
        inquiry_id="INQ-001",
        fulfillment_type=FulfillmentType.FROM_STOCK,
        stock_qty=500.0, production_qty=0.0,
        production_lead_days=0, transit_days=2,
        total_lead_time_days=2,
        customer_required_days=30, can_meet_deadline=True,
        delivery_location="Ludhiana", delivery_zone="North",
        location_found=True,
    )
    docs    = pd_mod.load_pricing_documents()
    pricing = pe_mod.compute_pricing(
        extraction, req_summary, qualification, feasibility, docs, client=None
    )
    draft = qb_mod.build_quotation(extraction, pricing, feasibility, qualification, customer)

    # ── Render HTML ──────────────────────────────────────────────────
    html = render_quotation_html(draft)
    out_path = os.path.join(os.path.dirname(__file__), f"quotation_{draft.inquiry_id}.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML quotation saved → {out_path}")

    # ── Persist to DB ─────────────────────────────────────────────────
    engine  = create_async_engine("sqlite+aiosqlite:///sales_os.db")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as session:
        record = await save_quotation(session, draft, html)
        print(f"\nDB record saved   : {record.id}")
        print(f"Status            : {record.status}")
        print(f"Requires approval : {record.requires_approval}")

        # ── Simulate human approval ───────────────────────────────────
        if record.requires_approval:
            print("\nSimulating human approval...")
            record = await approve_quotation(session, record, approved_by="sales_manager")
            print(f"Status after approval : {record.status}")

        # ── Dispatch via email ────────────────────────────────────────
        print(f"\nDispatching quotation...")
        result = await dispatch_quotation(
            session, record, draft,
            channel="email",
            recipient="ramesh@apexsteel.in",
        )
        print(f"\nDispatch result   : {result.success}")
        print(f"Message           : {result.message}")
        print(f"Final status      : {record.status}")


if __name__ == "__main__":
    asyncio.run(_demo())