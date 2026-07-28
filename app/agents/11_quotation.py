"""
Sub-problem: Quotation Builder

Assembles a complete, structured QuotationDraft from every upstream result.
Does NOT render HTML yet — that is 12_quotation_renderer.py.

Responsibilities:
  1. Generate unique quotation number
  2. Resolve payment terms from policy (customer_type + order value)
  3. Build line items from PricingResult
  4. Set delivery timeline from FeasibilityResult
  5. Attach warranty, freight terms, standard T&C
  6. Set status: PENDING_APPROVAL if any flag raised, else DRAFT

Depends on:
  inquiry_agent.py           → InquiryExtraction
  05_customer_lookup.py      → CustomerProfile
  06_customer_qualification  → QualificationResult
  08_feasibility_engine      → FeasibilityResult
  10_pricing_engine          → PricingResult

Design rule: ZERO LLM calls — pure data assembly.

Run:
    python 11_quotation_builder.py
"""

import io
import csv
import sys
import os
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel, Field
from app.rag.models import AgentRAGContext
from app.database.models.quotation import QuotationStatus

sys.path.insert(0, os.path.dirname(__file__))
ia        = import_module("01_Inquiry")  # for Base, log_action, InquiryExtraction
lk        = import_module("05_customer_qual")
qual_mod  = import_module("06_customer")
fe_mod    = import_module("08_feasiblity")
pe_mod    = import_module("10_pricing_agent")

InquiryExtraction   = ia.InquiryExtraction
CustomerProfile     = lk.CustomerProfile
CustomerType        = lk.CustomerType
QualificationResult = qual_mod.QualificationResult
FeasibilityResult   = fe_mod.FeasibilityResult
FulfillmentType     = fe_mod.FulfillmentType
PricingResult       = pe_mod.PricingResult


# -----------------------------------------------------------------------
# Configurable business defaults (move to settings table in prod)
# -----------------------------------------------------------------------
COMPANY_NAME      = "IndusSteel Trading Pvt. Ltd."
COMPANY_ADDRESS   = "Plot 14, Industrial Area Phase II, Ludhiana - 141003, Punjab"
COMPANY_GSTIN     = "03AABCI1234A1Z5"
COMPANY_EMAIL     = "sales@indussteel.in"
COMPANY_PHONE     = "+91-161-4567890"
COMPANY_BANK      = "HDFC Bank, A/C: 50200012345678, IFSC: HDFC0001234"

QUOTATION_VALIDITY_DAYS = 30
DEFAULT_WARRANTY   = (
    "Material supplied shall conform to the specified IS/BIS standard. "
    "Warranty is limited to replacement of non-conforming material on "
    "production of test certificate and physical inspection at our works."
)
DEFAULT_FREIGHT    = "Freight included up to delivery address mentioned above (road transport)."


# -----------------------------------------------------------------------
# Payment terms policy CSV
# -----------------------------------------------------------------------

SAMPLE_PAYMENT_TERMS_CSV = """\
customer_type,order_value_min,order_value_max,terms_code,terms_description
new,0,999999,"ADV_100","100% advance payment before dispatch"
new,1000000,4999999,"ADV_50","50% advance with order, balance before dispatch"
new,5000000,999999999,"ADV_30_LC","30% advance, balance by confirmed LC at sight"
existing,0,999999,"ADV_30_NET30","30% advance, balance net 30 days from dispatch"
existing,1000000,4999999,"ADV_20_NET45","20% advance, balance net 45 days from dispatch"
existing,5000000,999999999,"ADV_10_NET60","10% advance, balance net 60 days from dispatch"
"""

STANDARD_TNC = [
    "Prices are exclusive of GST. GST will be charged extra at applicable rates.",
    "Delivery is subject to availability of material and production schedule at the time of order confirmation.",
    "Any variation in government levies, duties, or GST rates after the quotation date will be charged extra.",
    "Force majeure events (strikes, natural calamities, government restrictions) are excluded from delivery commitments.",
    "Disputes, if any, are subject to jurisdiction of courts in Ludhiana, Punjab only.",
    "Payment should be made by RTGS/NEFT to the bank account mentioned. Cheques are not accepted.",
    "Material once dispatched will not be accepted back without prior written approval.",
    "This quotation supersedes all previous verbal or written communications for the same requirement.",
]


# -----------------------------------------------------------------------
# Output models
# -----------------------------------------------------------------------

# class QuotationStatus(str, Enum):
#     DRAFT            = "draft"             # assembled, not yet reviewed
#     PENDING_APPROVAL = "pending_approval"  # flagged, waiting for human
#     APPROVED         = "approved"          # human approved, ready to send
#     SENT             = "sent"              # dispatched to customer
#     REJECTED         = "rejected"          # human rejected the quotation


class QuotationLineItem(BaseModel):
    sr_no: int
    product_code: str
    description: str
    specification: str
    quantity: float
    unit: str
    unit_price_ex_gst: float
    discount_pct: float
    discounted_price_ex_gst: float
    gst_rate_pct: float
    gst_amount_per_unit: float
    total_inc_gst: float


class QuotationDraft(BaseModel):
    # Identifiers
    quotation_number: str = Field(default_factory=lambda: f"QT-{datetime.now().year}-{uuid.uuid4().hex[:4].upper()}")
    inquiry_id: str
    quotation_date: str = Field(default_factory=lambda: datetime.now().strftime("%d-%m-%Y"))
    valid_until: str = ""

    # Our company
    seller_name: str = COMPANY_NAME
    seller_address: str = COMPANY_ADDRESS
    seller_gstin: str = COMPANY_GSTIN
    seller_email: str = COMPANY_EMAIL
    seller_phone: str = COMPANY_PHONE
    seller_bank: str = COMPANY_BANK

    # Customer
    buyer_company: str
    buyer_contact: Optional[str] = None
    buyer_delivery_location: str
    buyer_gstin: Optional[str] = None

    # Line items
    line_items: list[QuotationLineItem] = []

    # Totals
    subtotal_ex_gst: float = 0.0
    total_gst_amount: float = 0.0
    total_inc_gst: float = 0.0

    # Commercial terms
    payment_terms_code: str = ""
    payment_terms_text: str = ""
    delivery_timeline: str = ""
    freight_terms: str = DEFAULT_FREIGHT
    warranty: str = DEFAULT_WARRANTY
    fulfillment_type: str = ""

    # T&C
    terms_and_conditions: list[str] = Field(default_factory=lambda: STANDARD_TNC.copy())

    # Approval
    status: QuotationStatus = QuotationStatus.DRAFT
    requires_human_approval: bool = False
    approval_reasons: list[str] = []
    prepared_by: str = "sales_agent"


# -----------------------------------------------------------------------
# Payment terms lookup
# -----------------------------------------------------------------------

def _load_payment_policies(csv_text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return sorted(
        [dict(r) for r in reader],
        key=lambda r: (r["customer_type"], float(r["order_value_min"]))
    )


def _get_payment_terms(
    policies: list[dict],
    customer_type: str,
    order_value: float,
) -> tuple[str, str]:
    """Returns (terms_code, terms_description)."""
    for p in policies:
        if (p["customer_type"] == customer_type and
                float(p["order_value_min"]) <= order_value <= float(p["order_value_max"])):
            return p["terms_code"], p["terms_description"]
    return "ADV_100", "100% advance payment before dispatch"


# -----------------------------------------------------------------------
# Delivery timeline builder — human-readable string
# -----------------------------------------------------------------------

def _build_delivery_timeline(feasibility: FeasibilityResult) -> str:
    ft = feasibility.fulfillment_type
    prod_days    = feasibility.production_lead_days
    transit_days = feasibility.transit_days
    total_days   = feasibility.total_lead_time_days

    if ft == FulfillmentType.FROM_STOCK:
        return (
            f"Material available ex-stock. "
            f"Dispatch within 2–3 working days of order + payment confirmation. "
            f"Transit: approx. {transit_days} day(s) to {feasibility.delivery_location}. "
            f"Total estimated delivery: {total_days} working days."
        )
    elif ft == FulfillmentType.FROM_PRODUCTION:
        return (
            f"Material against production. "
            f"Manufacturing lead time: {prod_days} working days. "
            f"Transit: approx. {transit_days} day(s) to {feasibility.delivery_location}. "
            f"Total estimated delivery: {total_days} working days from order + advance."
        )
    elif ft == FulfillmentType.PARTIAL_STOCK:
        return (
            f"Partial ex-stock ({feasibility.stock_qty:.0f} MT) dispatched within 2–3 days. "
            f"Balance ({feasibility.production_qty:.0f} MT) against production: "
            f"{prod_days} working days. "
            f"Transit: approx. {transit_days} day(s). "
            f"Full delivery in {total_days} working days."
        )
    else:
        return "Delivery timeline to be confirmed after engineering review."


# -----------------------------------------------------------------------
# Core: build_quotation()
# -----------------------------------------------------------------------

def build_quotation(
    extraction:    InquiryExtraction,
    pricing:       PricingResult,
    feasibility:   FeasibilityResult,
    qualification: QualificationResult,
    customer:      CustomerProfile,
    rag_context:   Optional[AgentRAGContext] = None,
) -> QuotationDraft:

    # ── Payment terms ──────────────────────────────────────────────────
    policies = _load_payment_policies(SAMPLE_PAYMENT_TERMS_CSV)
    terms_code, terms_text = _get_payment_terms(
        policies,
        customer_type=qualification.customer_type,
        order_value=pricing.total_invoice_value,
    )

    # ── Validity ──────────────────────────────────────────────────────
    valid_date = (datetime.now() + timedelta(days=QUOTATION_VALIDITY_DAYS)).strftime("%d-%m-%Y")

    # ── Line items ────────────────────────────────────────────────────
    line_items = []
    if pricing.pricing_possible:
        item = QuotationLineItem(
            sr_no=1,
            product_code=pricing.product_code or "",
            description=pricing.product_name or extraction.product_requested or "",
            specification=extraction.specifications or "As per standard IS specification",
            quantity=pricing.quantity_mt,
            unit="MT",
            unit_price_ex_gst=pricing.suggested_price_per_mt,
            discount_pct=pricing.applied_discount_pct,
            discounted_price_ex_gst=pricing.final_price_per_mt_ex_gst,
            gst_rate_pct=pricing.gst_rate_pct,
            gst_amount_per_unit=pricing.gst_per_mt,
            total_inc_gst=pricing.total_invoice_value,
        )
        line_items.append(item)

    # ── Approval status ───────────────────────────────────────────────
    all_reasons = list(set(
        pricing.approval_reasons + feasibility.human_review_reasons
    ))
    needs_approval = pricing.requires_human_approval or feasibility.requires_human_review
    status = QuotationStatus.PENDING_APPROVAL if needs_approval else QuotationStatus.DRAFT

    return QuotationDraft(
        inquiry_id=extraction.inquiry_id,
        valid_until=valid_date,

        # Customer block
        buyer_company=customer.company_name or qualification.company_name,
        buyer_contact=customer.contact_person or extraction.contact_person,
        buyer_delivery_location=extraction.delivery_location or "",
        buyer_gstin=customer.gstin,

        # Line items + totals
        line_items=line_items,
        subtotal_ex_gst=pricing.subtotal_ex_gst,
        total_gst_amount=pricing.gst_amount,
        total_inc_gst=pricing.total_invoice_value,

        # Commercial terms
        payment_terms_code=terms_code,
        payment_terms_text=terms_text,
        delivery_timeline=_build_delivery_timeline(feasibility),
        fulfillment_type=feasibility.fulfillment_type.value,

        # Approval
        status=status,
        requires_human_approval=needs_approval,
        approval_reasons=all_reasons,
    )


# -----------------------------------------------------------------------
# Demo
# -----------------------------------------------------------------------

if __name__ == "__main__":
    cat_mod  = import_module("03_catalog_ingestion")
    req_mod  = import_module("04_requirement_matching")
    inv_mod  = import_module("07_inventory_check")
    pd_mod   = import_module("09_pricing_documents")

    MatchType       = req_mod.MatchType
    FulfillmentType = fe_mod.FulfillmentType
    Priority        = qual_mod.Priority
    LeadTemperature = qual_mod.LeadTemperature

    # ── Mock upstream results (simulating INQ-001: Apex Steel, 500 MT MS Billet) ──
    extraction = InquiryExtraction(
        inquiry_id="INQ-001",
        customer_name="Ramesh Kumar",
        company_name="Apex Steel Pvt Ltd",
        contact_person="Ramesh Kumar",
        product_requested="MS Billet IS2062",
        quantity="500 MT",
        specifications="100x100mm square section",
        delivery_location="Ludhiana",
        delivery_date="within 30 days",
        payment_expectation="30 days credit",
        extraction_confidence=0.95,
    )
    product = cat_mod.CatalogProduct(
        product_code="MSB-001", name="MS Billet",
        category="Steel Billet", unit="MT",
    )
    req_summary = req_mod.RequirementSummary(
        inquiry_id="INQ-001", match_type=MatchType.EXACT,
        matched_product=product, similarity_score=0.92,
        summary_text="Exact catalog match found.",
    )
    customer = CustomerProfile(
        customer_type=CustomerType.EXISTING,
        customer_id="CUST-001",
        company_name="Apex Steel Pvt Ltd",
        contact_person="Ramesh Kumar",
        city="Ludhiana",
        gstin="03AABCA1234C1Z5",
        credit_limit=5_000_000,
        outstanding_amount=1_200_000,
        credit_utilization_pct=24.0,
        payment_behavior=lk.PaymentBehavior.GOOD,
    )
    qualification = QualificationResult(
        inquiry_id="INQ-001", company_name="Apex Steel Pvt Ltd",
        customer_type="existing", score=82,
        score_breakdown={}, temperature=LeadTemperature.HOT,
        priority=Priority.P1, rationale="Strong existing customer.",
    )
    feasibility = FeasibilityResult(
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

    draft = build_quotation(extraction, pricing, feasibility, qualification, customer)

    print(f"Quotation No : {draft.quotation_number}")
    print(f"Date         : {draft.quotation_date}  |  Valid until: {draft.valid_until}")
    print(f"Customer     : {draft.buyer_company}  |  GSTIN: {draft.buyer_gstin}")
    print(f"Status       : {draft.status.value.upper()}")
    print(f"Approval needed : {draft.requires_human_approval}")
    print(f"\nLine Items:")
    for li in draft.line_items:
        print(f"  {li.sr_no}. {li.description} ({li.product_code})")
        print(f"     Qty: {li.quantity} MT  |  "
              f"Unit price: ₹{li.unit_price_ex_gst:,.2f}  |  "
              f"Discount: {li.discount_pct:.1f}%")
        print(f"     Price ex-GST: ₹{li.discounted_price_ex_gst:,.2f}  |  "
              f"GST({li.gst_rate_pct:.0f}%): ₹{li.gst_amount_per_unit:,.2f}")
        print(f"     Total inc-GST: ₹{li.total_inc_gst:,.2f}")
    print(f"\nSubtotal (ex-GST) : ₹{draft.subtotal_ex_gst:>14,.2f}")
    print(f"GST               : ₹{draft.total_gst_amount:>14,.2f}")
    print(f"TOTAL INVOICE     : ₹{draft.total_inc_gst:>14,.2f}")
    print(f"\nPayment Terms  : {draft.payment_terms_text}")
    print(f"Delivery       : {draft.delivery_timeline}")
    print(f"Warranty       : {draft.warranty[:80]}...")
    print(f"\nT&C ({len(draft.terms_and_conditions)} clauses): {draft.terms_and_conditions[0][:70]}...")
