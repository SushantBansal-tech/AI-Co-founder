"""
Sub-problem: Handoff Builder

Assembles one HandoffPackage per department from all upstream results.
Every package contains:
  - priority level (P1/P2/P3 from qualification)
  - a job reference number
  - a plain-English summary
  - a concrete action item list
  - a deadline
  - department-specific structured data dict

Five departments:
  PRODUCTION   → job order, quantity to make, spec, deadline, mill target
  INVENTORY    → stock to reserve, warehouse, dispatch date, packing
  PURCHASE     → raw material required, specs, lead time (only if production needed)
  DISPATCH     → shipping details, documents list, transporter, e-way bill
  FINANCE      → invoice amount, GST type, payment terms, advance confirmation

Design rule: zero LLM calls — pure data transformation.
Gemini is only used in 21 for the consolidated narrative cover note.

Run:
    python 20_handoff_builder.py
"""

import sys
import os
import uuid
from datetime import datetime
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel, Field

sys.path.insert(0, os.path.dirname(__file__))
fe_mod  = import_module("08_feasiblity")
pe_mod  = import_module("10_pricing_agent")
po_mod  = import_module("18_PO")
qual_mod = import_module("06_customer")

FulfillmentType     = fe_mod.FulfillmentType
FeasibilityResult   = fe_mod.FeasibilityResult
PricingResult       = pe_mod.PricingResult
POExtraction        = po_mod.POExtraction
QualificationResult = qual_mod.QualificationResult


# ── Enums ─────────────────────────────────────────────────────────────────

class DepartmentType(str, Enum):
    PRODUCTION = "production"
    INVENTORY  = "inventory"
    PURCHASE   = "purchase"
    DISPATCH   = "dispatch"
    FINANCE    = "finance"


# ── Models ────────────────────────────────────────────────────────────────

class HandoffPackage(BaseModel):
    department:       DepartmentType
    priority:         str                    # "P1" | "P2" | "P3"
    job_reference:    str                    # e.g. "JO-2025-A1B2" or "DI-2025-A1B2"
    subject:          str                    # one-line heading
    summary:          str                    # 2-3 sentence plain-English overview
    action_items:     list[str]              # ordered list — what the team must do
    deadline:         Optional[str]          # hard date for this dept's work
    structured_data:  dict                   # machine-readable data for ERP/Slack/email


class HandoffSummary(BaseModel):
    handoff_id:       str = Field(default_factory=lambda: str(uuid.uuid4()))
    sales_order_id:   str
    po_number:        str
    quotation_number: str
    buyer_company:    str
    total_value:      float
    packages:         list[HandoffPackage]
    prepared_at:      str = Field(
                          default_factory=lambda: datetime.utcnow().strftime("%d-%m-%Y %H:%M UTC")
                      )
    prepared_by:      str = "handoff_agent"


# ── GST type helper ───────────────────────────────────────────────────────
# Indian GSTIN: first 2 digits = state code
# If buyer state == seller state → intra-state (CGST + SGST)
# Else → inter-state (IGST)

SELLER_STATE_CODE = "03"   # Punjab — from settings in prod

def _gst_type(buyer_gstin: Optional[str]) -> tuple[str, str]:
    """Returns (gst_type, note)."""
    if not buyer_gstin or len(buyer_gstin) < 2:
        return "IGST", "GSTIN not available — defaulting to IGST"
    buyer_state = buyer_gstin[:2]
    if buyer_state == SELLER_STATE_CODE:
        return "CGST+SGST", f"Intra-state supply (both Punjab {SELLER_STATE_CODE})"
    return "IGST", f"Inter-state supply (seller: {SELLER_STATE_CODE}, buyer: {buyer_state})"


# ── Date helpers ──────────────────────────────────────────────────────────

def _days_to_date(base_date: str, days: int) -> str:
    """
    Add `days` working days to base_date string.
    base_date format: "DD-MM-YYYY"
    """
    try:
        dt = datetime.strptime(base_date, "%d-%m-%Y")
        from datetime import timedelta
        result = dt + timedelta(days=days)
        return result.strftime("%d-%m-%Y")
    except Exception:
        return f"T+{days}d from {base_date}"


def _ref(prefix: str, quotation_number: str) -> str:
    suffix = quotation_number.replace("QT-", "").replace("-", "")[-6:]
    year   = datetime.now().year
    return f"{prefix}-{year}-{suffix}"


# ── Package builders ──────────────────────────────────────────────────────

def _build_production_package(
    po: POExtraction,
    feasibility: FeasibilityResult,
    pricing: PricingResult,
    qualification: QualificationResult,
    quotation_number: str,
) -> HandoffPackage:
    prod_qty   = feasibility.production_qty
    is_needed  = prod_qty > 0
    deadline   = po.delivery_date or f"T+{feasibility.total_lead_time_days}d"

    if not is_needed:
        return HandoffPackage(
            department=DepartmentType.PRODUCTION,
            priority=qualification.priority.value if hasattr(qualification.priority, 'value') else str(qualification.priority),
            job_reference=_ref("JO", quotation_number),
            subject=f"NO PRODUCTION NEEDED — {po.buyer_company}",
            summary=(
                f"Full order quantity ({feasibility.stock_qty:.0f} {po.unit or 'MT'}) "
                f"for {po.buyer_company} will be fulfilled from existing stock. "
                "No manufacturing run required for this order."
            ),
            action_items=[
                "Confirm stock reservation with Inventory team.",
                "No rolling mill action required for this PO.",
            ],
            deadline=deadline,
            structured_data={
                "production_required":  False,
                "production_qty":       0,
                "stock_qty":            feasibility.stock_qty,
                "fulfillment_type":     feasibility.fulfillment_type.value,
            },
        )

    # Production required
    lead_days  = feasibility.production_lead_days
    product    = f"{pricing.product_name} ({po.product_description or pricing.product_code})"
    spec       = po.product_description or pricing.product_name or "as per quotation"

    # Mill target: dispatch_date - transit - 2 days buffer
    transit    = feasibility.transit_days
    buffer     = 2
    mill_days  = lead_days - transit - buffer
    mill_target = f"T+{max(mill_days, 1)}d"

    actions = [
        f"Raise Production Job Order {_ref('JO', quotation_number)} for {prod_qty:.0f} {po.unit or 'MT'} of {product}.",
        f"Confirm raw material availability for {prod_qty:.0f} MT production run.",
        f"Schedule rolling mill — target completion by {mill_target} to meet dispatch deadline.",
        "Conduct quality check and issue Mill Test Certificate (MTC) before dispatch.",
        f"Coordinate with Inventory team for packing ({'; '.join(po.special_conditions[:2]) if po.special_conditions else 'standard bundling'}).",
    ]
    if po.special_conditions:
        for cond in po.special_conditions[:3]:
            actions.append(f"Note special condition: {cond}")

    return HandoffPackage(
        department=DepartmentType.PRODUCTION,
        priority=qualification.priority.value if hasattr(qualification.priority, 'value') else str(qualification.priority),
        job_reference=_ref("JO", quotation_number),
        subject=f"Production Order — {prod_qty:.0f} MT {pricing.product_name} for {po.buyer_company}",
        summary=(
            f"Production required: {prod_qty:.0f} {po.unit or 'MT'} of {product}. "
            f"Manufacturing lead time: {lead_days} working days. "
            f"Material must be ready for dispatch by {deadline} to meet customer delivery commitment."
        ),
        action_items=actions,
        deadline=deadline,
        structured_data={
            "production_required":   True,
            "production_qty":        prod_qty,
            "product":               product,
            "specification":         spec,
            "lead_time_days":        lead_days,
            "mill_target":           mill_target,
            "dispatch_deadline":     deadline,
            "special_conditions":    po.special_conditions,
        },
    )


def _build_inventory_package(
    po: POExtraction,
    feasibility: FeasibilityResult,
    qualification: QualificationResult,
    quotation_number: str,
) -> HandoffPackage:
    stock_qty  = feasibility.stock_qty
    dispatch_in = feasibility.transit_days + 1   # need to be ready 1 day before transit
    shipping_addr = po.shipping_address or po.delivery_location or feasibility.delivery_location

    actions = [
        f"Reserve {stock_qty:.0f} {po.unit or 'MT'} from {feasibility.delivery_location} warehouse for PO {po.po_number}.",
        "Conduct physical stock inspection — confirm grade, dimensions, and surface quality.",
        "Bundle material as per packing requirement and label each bundle with heat number.",
        f"Material ready-for-loading by dispatch date (T+{dispatch_in}d from today).",
        f"Shipping destination: {shipping_addr}.",
    ]
    if po.special_conditions:
        for cond in po.special_conditions[:2]:
            actions.append(f"Note PO condition: {cond}")

    return HandoffPackage(
        department=DepartmentType.INVENTORY,
        priority=qualification.priority.value if hasattr(qualification.priority, 'value') else str(qualification.priority),
        job_reference=_ref("WH", quotation_number),
        subject=f"Stock Reservation — {stock_qty:.0f} MT for {po.buyer_company}",
        summary=(
            f"Reserve and prepare {stock_qty:.0f} {po.unit or 'MT'} from warehouse stock. "
            f"Material must be ready for loading within {dispatch_in} days. "
            f"Destination: {shipping_addr}."
        ),
        action_items=actions,
        deadline=po.delivery_date,
        structured_data={
            "stock_qty":           stock_qty,
            "warehouse":           feasibility.delivery_location,
            "delivery_location":   shipping_addr,
            "ready_in_days":       dispatch_in,
            "packing_instruction": po.special_conditions,
        },
    )


def _build_purchase_package(
    po: POExtraction,
    feasibility: FeasibilityResult,
    qualification: QualificationResult,
    pricing: PricingResult,
    quotation_number: str,
) -> HandoffPackage:
    prod_qty = feasibility.production_qty
    if prod_qty <= 0:
        # No production → no raw material procurement needed
        return HandoffPackage(
            department=DepartmentType.PURCHASE,
            priority=qualification.priority.value if hasattr(qualification.priority, 'value') else str(qualification.priority),
            job_reference=_ref("PUR", quotation_number),
            subject=f"NO RM PROCUREMENT NEEDED — {po.buyer_company}",
            summary="Order fulfilled from existing stock. No raw material procurement required.",
            action_items=["No action required — ex-stock fulfillment."],
            deadline=None,
            structured_data={"procurement_required": False},
        )

    # Rough RM qty: add 5% wastage margin
    rm_qty      = round(prod_qty * 1.05, 1)
    rm_cost_est = round(pricing.rm_cost_per_mt * prod_qty, 0)

    actions = [
        f"Procure {rm_qty:.1f} MT of raw material (billets/scrap) for {prod_qty:.0f} MT production run.",
        f"Specification: suitable for {pricing.product_name} as per {po.product_description or 'IS2062'}.",
        f"Budget estimate: ₹{rm_cost_est:,.0f} (at ₹{pricing.rm_cost_per_mt:,.0f}/MT).",
        "Get at least 2 vendor quotes before ordering.",
        f"Raw material must arrive at plant within {max(feasibility.production_lead_days - 5, 2)} days.",
        "Raise Purchase Order to approved vendor after confirmation.",
    ]

    return HandoffPackage(
        department=DepartmentType.PURCHASE,
        priority=qualification.priority.value if hasattr(qualification.priority, 'value') else str(qualification.priority),
        job_reference=_ref("PUR", quotation_number),
        subject=f"RM Procurement — {rm_qty:.0f} MT for {pricing.product_name} ({po.buyer_company})",
        summary=(
            f"Raw material procurement needed for {prod_qty:.0f} MT production order. "
            f"Procure {rm_qty:.0f} MT (incl. 5% wastage) at est. ₹{rm_cost_est:,.0f}. "
            f"Must arrive before production start deadline."
        ),
        action_items=actions,
        deadline=po.delivery_date,
        structured_data={
            "procurement_required":    True,
            "rm_qty_mt":               rm_qty,
            "rm_spec":                 po.product_description or "IS2062",
            "rm_budget_estimate":      rm_cost_est,
            "rm_cost_per_mt":          pricing.rm_cost_per_mt,
            "required_by_days":        max(feasibility.production_lead_days - 5, 2),
        },
    )


def _build_dispatch_package(
    po: POExtraction,
    feasibility: FeasibilityResult,
    qualification: QualificationResult,
    pricing: PricingResult,
    quotation_number: str,
) -> HandoffPackage:
    total_qty      = (feasibility.stock_qty or 0) + (feasibility.production_qty or 0)
    shipping_addr  = po.shipping_address or po.delivery_location or feasibility.delivery_location
    trucks_needed  = max(1, round(total_qty / 20))  # approx 20 MT per truck
    gst_type, _    = _gst_type(po.buyer_gstin)

    actions = [
        f"Book {trucks_needed} truck(s) for {total_qty:.0f} MT to {shipping_addr}.",
        "Confirm lorry details with production/inventory teams 2 days before dispatch.",
        f"Prepare Sales Invoice (₹{pricing.total_invoice_value:,.0f} incl. {gst_type}).",
        f"Generate E-way bill before loading (mandatory for > ₹50,000).",
        "Obtain Lorry Receipt (LR) and share with customer and finance.",
        "Collect Mill Test Certificate(s) from production before dispatch.",
        f"Ensure PO number {po.po_number} is referenced on all dispatch documents.",
    ]
    if po.special_conditions:
        for cond in po.special_conditions[:3]:
            actions.append(f"Comply with PO condition: {cond}")

    required_docs = [
        "GST Tax Invoice",
        f"E-way Bill ({gst_type})",
        "Lorry Receipt (LR / GRN)",
        "Mill Test Certificate (MTC)",
        "Packing List",
        "Weighment Slip",
    ]

    return HandoffPackage(
        department=DepartmentType.DISPATCH,
        priority=qualification.priority.value if hasattr(qualification.priority, 'value') else str(qualification.priority),
        job_reference=_ref("DI", quotation_number),
        subject=f"Dispatch Order — {total_qty:.0f} MT to {shipping_addr}",
        summary=(
            f"Arrange dispatch of {total_qty:.0f} MT to {shipping_addr} "
            f"via approx. {trucks_needed} truck(s). "
            f"Delivery deadline: {po.delivery_date or 'as per quotation'}. "
            f"All {len(required_docs)} required documents must be ready before loading."
        ),
        action_items=actions,
        deadline=po.delivery_date,
        structured_data={
            "total_qty":          total_qty,
            "shipping_address":   shipping_addr,
            "trucks_needed":      trucks_needed,
            "delivery_deadline":  po.delivery_date,
            "gst_type":           gst_type,
            "po_number":          po.po_number,
            "buyer_gstin":        po.buyer_gstin,
            "required_documents": required_docs,
            "special_conditions": po.special_conditions,
        },
    )


def _build_finance_package(
    po: POExtraction,
    feasibility: FeasibilityResult,
    pricing: PricingResult,
    qualification: QualificationResult,
    quotation_number: str,
) -> HandoffPackage:
    gst_type, gst_note = _gst_type(po.buyer_gstin)
    gst_amt    = pricing.gst_amount
    base_amt   = pricing.subtotal_ex_gst
    total_amt  = pricing.total_invoice_value
    pay_terms  = po.payment_terms or pricing.__class__.__name__

    # Advance and balance split
    # Parse advance % from payment terms (look for "20% advance" pattern)
    import re
    advance_pct_match = re.search(r"(\d+)\s*%\s*advance", (po.payment_terms or ""), re.IGNORECASE)
    advance_pct = int(advance_pct_match.group(1)) if advance_pct_match else 20
    advance_amt = round(total_amt * advance_pct / 100, 0)
    balance_amt = round(total_amt - advance_amt, 0)

    actions = [
        f"Confirm receipt of advance payment ₹{advance_amt:,.0f} ({advance_pct}%) before dispatch clearance.",
        f"Generate GST Tax Invoice — Base: ₹{base_amt:,.0f} + {gst_type} {pricing.gst_rate_pct:.0f}%: ₹{gst_amt:,.0f} = Total: ₹{total_amt:,.0f}.",
        f"Note: {gst_note}.",
        f"Raise E-way bill for inter/intra-state movement.",
        f"Set payment follow-up reminder for balance ₹{balance_amt:,.0f} as per terms: {po.payment_terms or 'per quotation'}.",
        f"Update accounts receivable with PO {po.po_number} — {po.buyer_company} (GSTIN: {po.buyer_gstin or 'N/A'}).",
        "Confirm customer credit limit check before dispatch authorisation.",
    ]

    return HandoffPackage(
        department=DepartmentType.FINANCE,
        priority=qualification.priority.value if hasattr(qualification.priority, 'value') else str(qualification.priority),
        job_reference=_ref("FI", quotation_number),
        subject=f"Invoice & Payment — ₹{total_amt:,.0f} | {po.buyer_company}",
        summary=(
            f"Sales invoice of ₹{total_amt:,.0f} (incl. {gst_type} {pricing.gst_rate_pct:.0f}%) "
            f"against PO {po.po_number} from {po.buyer_company}. "
            f"Advance: ₹{advance_amt:,.0f} ({advance_pct}%); Balance: ₹{balance_amt:,.0f} due as per terms."
        ),
        action_items=actions,
        deadline=po.delivery_date,
        structured_data={
            "po_number":           po.po_number,
            "buyer_company":       po.buyer_company,
            "buyer_gstin":         po.buyer_gstin,
            "base_amount":         base_amt,
            "gst_type":            gst_type,
            "gst_rate_pct":        pricing.gst_rate_pct,
            "gst_amount":          gst_amt,
            "total_invoice":       total_amt,
            "advance_pct":         advance_pct,
            "advance_amount":      advance_amt,
            "balance_amount":      balance_amt,
            "payment_terms":       po.payment_terms,
            "quotation_number":    quotation_number,
        },
    )


# ── Master builder ────────────────────────────────────────────────────────

def build_all_packages(
    sales_order_id: str,
    po: POExtraction,
    feasibility: FeasibilityResult,
    pricing: PricingResult,
    qualification: QualificationResult,
    quotation_number: str,
) -> HandoffSummary:
    packages = [
        _build_production_package(po, feasibility, pricing, qualification, quotation_number),
        _build_inventory_package(po, feasibility, qualification, quotation_number),
        _build_purchase_package(po, feasibility, qualification, pricing, quotation_number),
        _build_dispatch_package(po, feasibility, qualification, pricing, quotation_number),
        _build_finance_package(po, feasibility, pricing, qualification, quotation_number),
    ]
    return HandoffSummary(
        sales_order_id=sales_order_id,
        po_number=po.po_number or "N/A",
        quotation_number=quotation_number,
        buyer_company=po.buyer_company or "Unknown",
        total_value=pricing.total_invoice_value,
        packages=packages,
    )


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    po = POExtraction(
        po_number="APX-PO-2025-0891",
        po_date="15-06-2025",
        buyer_company="Apex Steel Pvt Ltd",
        buyer_gstin="03AABCA1234C1Z5",
        billing_address="Plot 22, Industrial Area, Ludhiana",
        shipping_address="Apex Steel Works, Village Sahnewal, Ludhiana - 141120",
        product_description="MS Billet IS2062 Grade, 100x100mm Square Section",
        quantity=500.0,
        unit="MT",
        price_per_unit_ex_gst=14000.0,
        gst_rate_pct=18.0,
        gst_amount=1_260_000.0,
        total_amount_inc_gst=8_260_000.0,
        payment_terms="20% advance with PO, balance 80% within 45 days of dispatch",
        delivery_date="30-06-2025",
        delivery_location="Ludhiana",
        special_conditions=[
            "Material must be accompanied by original mill test certificate.",
            "Each bundle to be labelled with heat number and grade.",
            "Packaging: bundles of 2 MT each.",
        ],
        extraction_confidence=0.97,
    )
    feasibility = FeasibilityResult(
        inquiry_id="INQ-001",
        fulfillment_type=FulfillmentType.PARTIAL_STOCK,
        stock_qty=150.0, production_qty=350.0,
        production_lead_days=14, transit_days=2,
        total_lead_time_days=16,
        customer_required_days=15, can_meet_deadline=True,
        delivery_location="Ludhiana", delivery_zone="North",
        location_found=True,
    )
    pricing = pe_mod.PricingResult(
        inquiry_id="INQ-001", product_code="MSB-001",
        product_name="MS Billet IS2062", quantity_mt=500.0,
        rm_cost_per_mt=11500, overhead_per_mt=920,
        transport_per_mt=450, total_cost_per_mt=12870,
        list_price_per_mt=14500, floor_price_per_mt=13989,
        suggested_price_per_mt=14500, customer_type="existing",
        applied_discount_pct=3.5, max_discount_pct=12.0,
        approval_limit_pct=8.0, discounted_price_per_mt=14000,
        min_margin_pct=8.0, target_margin_pct=15.0, actual_margin_pct=8.9,
        gst_rate_pct=18.0, gst_per_mt=2520, final_price_per_mt_ex_gst=14000,
        final_price_per_mt_inc_gst=16520, subtotal_ex_gst=7_000_000,
        gst_amount=1_260_000, total_invoice_value=8_260_000, pricing_possible=True,
    )
    qualification = qual_mod.QualificationResult(
        inquiry_id="INQ-001", company_name="Apex Steel Pvt Ltd",
        customer_type="existing", score=82, score_breakdown={},
        temperature=qual_mod.LeadTemperature.HOT,
        priority=qual_mod.Priority.P1,
        rationale="High-value existing customer.",
    )

    summary = build_all_packages(
        "SO-DEMO-001", po, feasibility, pricing, qualification, "QT-2025-A1B2"
    )

    print(f"HANDOFF SUMMARY  —  {summary.handoff_id[:8]}")
    print(f"PO: {summary.po_number}  |  Buyer: {summary.buyer_company}")
    print(f"Total Invoice: ₹{summary.total_value:,.0f}")
    print(f"Packages: {len(summary.packages)}")

    for pkg in summary.packages:
        print(f"\n{'─'*60}")
        print(f"[{pkg.department.value.upper()}]  {pkg.job_reference}  |  {pkg.priority}")
        print(f"Subject   : {pkg.subject}")
        print(f"Summary   : {pkg.summary}")
        print(f"Deadline  : {pkg.deadline or 'N/A'}")
        print("Actions   :")
        for i, action in enumerate(pkg.action_items, 1):
            print(f"  {i:2}. {action}")