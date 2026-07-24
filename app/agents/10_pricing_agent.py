"""
Sub-problem: Pricing Engine

Takes PricingDocuments + all upstream results and produces a PricingResult with:
  1. Full cost breakdown (RM + overhead + transport = total cost/MT)
  2. Floor price    — minimum price to maintain min_margin_pct (never quote below)
  3. List price     — from price list CSV (standard catalog price)
  4. Suggested price — higher of (list_price, floor_price)
  5. Auto discount  — applied from policy based on customer_type + order value
  6. Final price    — after discount, enforced above floor
  7. GST calculation
  8. Total invoice value
  9. Approval flag  — if discount > approval_limit OR margin < minimum
  10. price_logic   — every calculation step stored for full transparency

Human approval triggers (per spec):
  - Applied discount > approval_limit_pct in the policy band
  - Final margin < min_margin_pct
  - Order value > ₹50L (large order threshold — configurable)

LLM (Gemini) is ONLY used to write the plain-English price explanation.
Every number is computed deterministically.

Depends on:
  inquiry_agent.py           → InquiryExtraction
  04_requirement_matching    → RequirementSummary
  05_customer_lookup         → CustomerType
  06_customer_qualification  → QualificationResult
  08_feasibility_engine      → FeasibilityResult
  09_pricing_documents       → PricingDocuments, get_discount_band, get_gst_rate

Run:
    GEMINI_API_KEY=xxx python 10_pricing_engine.py
    (works without key — fallback explanation used)
"""

import os
import sys
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel
from google import genai
from app.rag.models import AgentRAGContext

sys.path.insert(0, os.path.dirname(__file__))
ia       = import_module("01_Inquiry")  # for Base, log_action, InquiryExtraction
req_mod  = import_module("04_requirment")
qual_mod = import_module("06_customer")
fe_mod   = import_module("08_feasiblity")
pd_mod   = import_module("09_pricing")
inv_mod  = import_module("07_inventory")

InquiryExtraction    = ia.InquiryExtraction
RequirementSummary   = req_mod.RequirementSummary
QualificationResult  = qual_mod.QualificationResult
LeadTemperature      = qual_mod.LeadTemperature
FeasibilityResult    = fe_mod.FeasibilityResult
PricingDocuments     = pd_mod.PricingDocuments
get_discount_band    = pd_mod.get_discount_band
get_gst_rate         = pd_mod.get_gst_rate
parse_quantity       = inv_mod.parse_quantity

# Configurable thresholds (move to config/env in prod)
LARGE_ORDER_THRESHOLD_INR = 5_000_000   # ₹50L — triggers human review
DEFAULT_TRANSPORT_ZONE    = "Unknown"


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class ApprovalReason(str, Enum):
    DISCOUNT_EXCEEDS_LIMIT = "discount_exceeds_approval_limit"
    LOW_MARGIN             = "margin_below_minimum"
    LARGE_ORDER            = "large_order_value"
    CUSTOM_PRODUCT         = "custom_product_no_price"


class PricingResult(BaseModel):
    inquiry_id: str
    product_code: Optional[str]
    product_name: Optional[str]
    quantity_mt: float

    # ── Cost components (per MT) ──────────────────────────────────────────
    rm_cost_per_mt: float = 0.0
    overhead_per_mt: float = 0.0
    transport_per_mt: float = 0.0
    total_cost_per_mt: float = 0.0

    # ── Pricing (per MT, ex-GST) ──────────────────────────────────────────
    list_price_per_mt: float = 0.0        # from price list CSV
    floor_price_per_mt: float = 0.0       # cost / (1 - min_margin/100)
    suggested_price_per_mt: float = 0.0   # max(list_price, floor_price)

    # ── Discount ──────────────────────────────────────────────────────────
    customer_type: str = "new"
    discount_band_label: str = ""
    max_discount_pct: float = 0.0
    approval_limit_pct: float = 0.0
    applied_discount_pct: float = 0.0     # what we're actually offering
    discounted_price_per_mt: float = 0.0

    # ── Margin check ──────────────────────────────────────────────────────
    min_margin_pct: float = 0.0
    target_margin_pct: float = 0.0
    actual_margin_pct: float = 0.0

    # ── GST & totals ──────────────────────────────────────────────────────
    gst_rate_pct: float = 18.0
    gst_per_mt: float = 0.0
    final_price_per_mt_ex_gst: float = 0.0
    final_price_per_mt_inc_gst: float = 0.0

    subtotal_ex_gst: float = 0.0
    gst_amount: float = 0.0
    total_invoice_value: float = 0.0

    # ── Approval flags ────────────────────────────────────────────────────
    requires_human_approval: bool = False
    approval_reasons: list[str] = []
    can_proceed_without_approval: bool = True

    # ── Full audit trail ──────────────────────────────────────────────────
    price_logic: dict = {}          # every calculation step, shown in quotation
    pricing_possible: bool = True   # False for CUSTOM products with no price list
    explanation: str = ""           # plain-English explanation (LLM or fallback)


# ---------------------------------------------------------------------------
# Step 1: Cost build-up
# ---------------------------------------------------------------------------

def _compute_cost(
    product_code: str,
    delivery_zone: str,
    docs: PricingDocuments,
) -> tuple[float, float, float, float, dict]:
    """
    Returns (rm_cost, overhead, transport, total_cost, cost_logic_dict).
    All per MT.
    """
    rm_entry = docs.rm_costs.get(product_code)
    transport_cost = docs.transport_costs.get(delivery_zone,
                     docs.transport_costs.get(DEFAULT_TRANSPORT_ZONE, 900.0))

    if rm_entry is None:
        raise ValueError(
        f"RM cost not found for product {product_code}"
    )

    overhead   = rm_entry.overhead_per_mt
    total_cost = rm_entry.rm_cost_per_mt + overhead + transport_cost

    logic = {
        "rm_cost_per_mt":           rm_entry.rm_cost_per_mt,
        "manufacturing_overhead_pct": rm_entry.manufacturing_overhead_pct,
        "overhead_per_mt":          round(overhead, 2),
        "transport_zone":           delivery_zone,
        "transport_per_mt":         transport_cost,
        "total_cost_per_mt":        round(total_cost, 2),
    }
    return rm_entry.rm_cost_per_mt, overhead, transport_cost, total_cost, logic


# ---------------------------------------------------------------------------
# Step 2: Selling price (list vs floor)
# ---------------------------------------------------------------------------

def _compute_selling_price(
    product_code: str,
    total_cost_per_mt: float,
    docs: PricingDocuments,
) -> tuple[float, float, float, float, dict]:
    """
    Returns (list_price, floor_price, suggested_price, target_margin, logic).
    """
    pl_entry     = docs.price_list.get(product_code)
    margin_rule  = docs.margin_rules.get(product_code)

    list_price     = pl_entry.base_price_per_mt      if pl_entry    else 0.0
    min_margin     = margin_rule.min_margin_pct       if margin_rule else 10.0
    target_margin  = margin_rule.target_margin_pct    if margin_rule else 15.0

    # Floor price: minimum to keep margin above min_margin_pct
    floor_price = round(total_cost_per_mt / (1 - min_margin / 100), 2) if total_cost_per_mt > 0 else 0.0

    # Suggested = highest of list price and floor (never quote below cost)
    suggested_price = round(max(list_price, floor_price), 2)

    logic = {
        "list_price_per_mt":    list_price,
        "min_margin_pct":       min_margin,
        "target_margin_pct":    target_margin,
        "floor_price_per_mt":   floor_price,
        "floor_price_formula":  f"{total_cost_per_mt} / (1 - {min_margin}%) = {floor_price}",
        "suggested_price_per_mt": suggested_price,
        "price_basis":          ("list_price" if list_price >= floor_price else "floor_price (list below cost+margin)"),
    }
    return list_price, floor_price, suggested_price, min_margin, target_margin, logic


# ---------------------------------------------------------------------------
# Step 3: Discount application
# ---------------------------------------------------------------------------

def _apply_discount(
    suggested_price: float,
    floor_price: float,
    total_cost: float,
    order_value_at_list: float,
    customer_type: str,
    docs: PricingDocuments,
) -> tuple[float, float, float, float, float, bool, str, dict]:
    """
    Returns (applied_discount_pct, discounted_price, actual_margin_pct,
             max_discount, approval_limit, needs_approval, band_label, logic).

    Auto-applies the maximum allowed discount without approval as a gesture of
    goodwill — sales team can lower it manually before sending the quotation.
    """
    band = get_discount_band(docs, customer_type, order_value_at_list)

    if band is None:
        # No policy found — no discount
        return (0.0, suggested_price,
                round((suggested_price - total_cost) / suggested_price * 100, 2),
                0.0, 0.0, False, "No discount band found", {"note": "No matching discount policy"})

    # Auto-apply the approval_limit (max without sign-off)
    # The sales team may choose a higher discount subject to approval
    auto_discount = band.approval_limit_pct
    discounted_price = round(suggested_price * (1 - auto_discount / 100), 2)

    # Safety: never let discount push price below floor
    if discounted_price < floor_price:
        discounted_price = floor_price
        auto_discount = round((1 - floor_price / suggested_price) * 100, 2)

    actual_margin = round((discounted_price - total_cost) / discounted_price * 100, 2)
    needs_approval = False  # auto_discount is within limit by construction

    band_label = (f"{customer_type}, ₹{band.order_value_min/1e5:.0f}L–"
                  f"₹{band.order_value_max/1e5:.0f}L  →  "
                  f"max {band.max_discount_pct}%  (auto: {band.approval_limit_pct}%)")

    logic = {
        "customer_type":         customer_type,
        "order_value_at_list":   order_value_at_list,
        "discount_band":         band_label,
        "max_discount_pct":      band.max_discount_pct,
        "approval_limit_pct":    band.approval_limit_pct,
        "auto_applied_discount": auto_discount,
        "discounted_price_per_mt": discounted_price,
        "actual_margin_pct":     actual_margin,
        "note": ("Discount capped at floor_price" if discounted_price == floor_price else "Standard auto-discount applied"),
    }
    return (auto_discount, discounted_price, actual_margin,
            band.max_discount_pct, band.approval_limit_pct,
            needs_approval, band_label, logic)


# ---------------------------------------------------------------------------
# Step 4: GST + invoice totals
# ---------------------------------------------------------------------------

def _compute_totals(
    final_price_ex_gst: float,
    quantity_mt: float,
    product_category: str,
    docs: PricingDocuments,
) -> tuple[float, float, float, float, float, dict]:
    """Returns (gst_rate, gst_per_mt, price_inc_gst, subtotal, gst_amount, total, logic)."""
    gst_rate      = get_gst_rate(docs, product_category)
    gst_per_mt    = round(final_price_ex_gst * gst_rate / 100, 2)
    price_inc_gst = round(final_price_ex_gst + gst_per_mt, 2)
    subtotal      = round(final_price_ex_gst * quantity_mt, 2)
    gst_amount    = round(gst_per_mt * quantity_mt, 2)
    total         = round(subtotal + gst_amount, 2)

    logic = {
        "gst_rate_pct":             gst_rate,
        "gst_per_mt":               gst_per_mt,
        "final_price_per_mt_ex_gst": final_price_ex_gst,
        "final_price_per_mt_inc_gst": price_inc_gst,
        "quantity_mt":              quantity_mt,
        "subtotal_ex_gst":          subtotal,
        "gst_amount":               gst_amount,
        "total_invoice_value":      total,
    }
    return gst_rate, gst_per_mt, price_inc_gst, subtotal, gst_amount, total, logic


# ---------------------------------------------------------------------------
# Step 5: Approval flag assembly
# ---------------------------------------------------------------------------

def _check_approval_needed(
    applied_discount: float,
    approval_limit: float,
    actual_margin: float,
    min_margin: float,
    total_invoice: float,
) -> tuple[bool, list[str]]:
    reasons = []
    if applied_discount > approval_limit:
        reasons.append(
            f"{ApprovalReason.DISCOUNT_EXCEEDS_LIMIT.value}: "
            f"applied {applied_discount:.1f}% > limit {approval_limit:.1f}%"
        )
    if actual_margin < min_margin:
        reasons.append(
            f"{ApprovalReason.LOW_MARGIN.value}: "
            f"actual margin {actual_margin:.1f}% < minimum {min_margin:.1f}%"
        )
    if total_invoice > LARGE_ORDER_THRESHOLD_INR:
        reasons.append(
            f"{ApprovalReason.LARGE_ORDER.value}: "
            f"invoice ₹{total_invoice/1e5:.1f}L exceeds ₹{LARGE_ORDER_THRESHOLD_INR/1e5:.0f}L threshold"
        )
    return bool(reasons), reasons


# ---------------------------------------------------------------------------
# LLM narrative — explain the pricing in plain English
# ---------------------------------------------------------------------------

PRICE_EXPLANATION_PROMPT = """\
Write a concise price justification (4-5 sentences) for an industrial B2B sales executive
to understand how we arrived at this quote. Be factual and clear. No fluff.

Product   : {product}
Quantity  : {qty} MT
Cost/MT   : ₹{cost:,.0f} (RM + overhead + transport)
List price: ₹{list_price:,.0f}/MT
Floor price: ₹{floor_price:,.0f}/MT (min {min_margin}% margin)
Offered   : ₹{offered:,.0f}/MT ex-GST ({discount}% discount applied)
Margin    : {margin:.1f}%
GST ({gst}%) : ₹{gst_amt:,.0f}
Total invoice: ₹{total:,.0f}
{approval_note}

Include: how cost was built up, why this price, what the discount is, total with GST.
"""

def _generate_explanation(
    result: PricingResult,
    client: Optional[genai.Client],
    rag_context: Optional[AgentRAGContext] = None,
) -> str:
    approval_note = (
        "⚠ REQUIRES HUMAN APPROVAL: " + "; ".join(result.approval_reasons)
        if result.requires_human_approval else "✓ Within auto-approval limits."
    )
    fallback = (
        f"Pricing for {result.product_name} ({result.quantity_mt} MT): "
        f"Total cost ₹{result.total_cost_per_mt:,.0f}/MT. "
        f"List price ₹{result.list_price_per_mt:,.0f}/MT, floor ₹{result.floor_price_per_mt:,.0f}/MT. "
        f"Offered at ₹{result.final_price_per_mt_ex_gst:,.0f}/MT after {result.applied_discount_pct:.1f}% discount. "
        f"Margin: {result.actual_margin_pct:.1f}%. "
        f"Total invoice ₹{result.total_invoice_value:,.0f} (incl. {result.gst_rate_pct:.0f}% GST). "
        f"{approval_note}"
    )
    if client is None:
        return fallback
    try:
        prompt = PRICE_EXPLANATION_PROMPT.format(
            product=result.product_name or "N/A",
            qty=result.quantity_mt,
            cost=result.total_cost_per_mt,
            list_price=result.list_price_per_mt,
            floor_price=result.floor_price_per_mt,
            min_margin=result.min_margin_pct,
            offered=result.final_price_per_mt_ex_gst,
            discount=result.applied_discount_pct,
            margin=result.actual_margin_pct,
            gst=result.gst_rate_pct,
            gst_amt=result.gst_amount,
            total=result.total_invoice_value,
            approval_note=approval_note,
        )
        if rag_context:
            prompt += (
                "\n\nRETRIEVED PRICING EVIDENCE:\n"
                f"{rag_context.combined_text}\n"
                "Use this only to explain applicable policies. The calculated "
                "prices, margins, taxes, and approval flags are authoritative."
            )
        response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return response.text.strip()
    except Exception:
        return fallback
    
def _missing_pricing_inputs(
    *,
    product_code: str,
    product_category: str,
    delivery_zone: str,
    customer_type: str,
    quantity_mt: float,
    docs: PricingDocuments,
) -> list[str]:
    missing: list[str] = []

    if product_code not in docs.price_list:
        missing.append(
            f"Price-list entry missing for {product_code}"
        )

    if product_code not in docs.rm_costs:
        missing.append(
            f"Product-level RM cost missing for {product_code}"
        )

    if product_code not in docs.margin_rules:
        missing.append(
            f"Margin rule missing for {product_code}"
        )

    if product_category not in docs.gst_rates:
        missing.append(
            f"GST rule missing for category {product_category}"
        )

    if delivery_zone not in docs.transport_costs:
        missing.append(
            f"Transport rate missing for zone {delivery_zone}"
        )

    price_entry = docs.price_list.get(product_code)

    if price_entry:
        provisional_order_value = (
            price_entry.base_price_per_mt * quantity_mt
        )

        discount_band = get_discount_band(
            docs,
            customer_type,
            provisional_order_value,
        )

        if discount_band is None:
            missing.append(
                "No discount policy for "
                f"customer_type={customer_type}, "
                f"order_value={provisional_order_value}"
            )

    return missing


# ---------------------------------------------------------------------------
# Main entry: compute_pricing()
# ---------------------------------------------------------------------------

def compute_pricing(
    extraction:    InquiryExtraction,
    requirement:   RequirementSummary,
    qualification: QualificationResult,
    feasibility:   FeasibilityResult,
    docs:          PricingDocuments,
    client:        Optional[genai.Client] = None,
    rag_context:   Optional[AgentRAGContext] = None,
) -> PricingResult:

    matched    = requirement.matched_product
    qty_mt, _  = parse_quantity(extraction.quantity)

    # Cannot price without a matched catalog product
    if matched is None:
        return PricingResult(
            inquiry_id=extraction.inquiry_id,
            product_code=None,
            product_name=extraction.product_requested,
            quantity_mt=qty_mt,
            pricing_possible=False,
            requires_human_approval=True,
            approval_reasons=[ApprovalReason.CUSTOM_PRODUCT_NO_PRICE.value
                               if hasattr(ApprovalReason, 'CUSTOM_PRODUCT_NO_PRICE')
                               else "custom_product_no_price — no price list entry"],
            can_proceed_without_approval=False,
            explanation="Cannot compute pricing: no catalog match. Custom product requires manual pricing.",
        )

    product_code     = matched.product_code
    product_category = matched.category
    delivery_zone    = feasibility.delivery_zone or DEFAULT_TRANSPORT_ZONE
    customer_type    = qualification.customer_type  # "new" or "existing"
    
    missing_inputs = _missing_pricing_inputs(
    product_code=product_code,
    product_category=product_category,
    delivery_zone=delivery_zone,
    customer_type=customer_type,
    quantity_mt=qty_mt,
    docs=docs,
  )

    if missing_inputs:
     return PricingResult(
        inquiry_id=extraction.inquiry_id,
        product_code=product_code,
        product_name=matched.name,
        quantity_mt=qty_mt,
        pricing_possible=False,
        requires_human_approval=True,
        approval_reasons=missing_inputs,
        can_proceed_without_approval=False,
        price_logic={
            "validation": {
                "status": "failed",
                "missing_inputs": missing_inputs,
            }
        },
        explanation=(
            "Pricing was stopped because required pricing "
            "documents are missing or incompatible: "
            + "; ".join(missing_inputs)
        ),
    )

    # ── Step 1: Cost build-up ────────────────────────────────────────────
    rm_cost, overhead, transport, total_cost, cost_logic = _compute_cost(
        product_code, delivery_zone, docs
    )

    # ── Step 2: Selling price ─────────────────────────────────────────────
    list_price, floor_price, suggested_price, min_margin, target_margin, price_logic = \
        _compute_selling_price(product_code, total_cost, docs)

    # ── Step 3: Discount ──────────────────────────────────────────────────
    order_value_at_list = round(suggested_price * qty_mt, 2)
    (applied_discount, discounted_price, actual_margin,
     max_discount, approval_limit, _, band_label, disc_logic) = _apply_discount(
        suggested_price, floor_price, total_cost,
        order_value_at_list, customer_type, docs
    )

    # ── Step 4: GST + totals ─────────────────────────────────────────────
    gst_rate, gst_per_mt, price_inc_gst, subtotal, gst_amount, total, total_logic = \
        _compute_totals(discounted_price, qty_mt, product_category, docs)

    # ── Step 5: Approval flags ────────────────────────────────────────────
    needs_approval, approval_reasons = _check_approval_needed(
        applied_discount, approval_limit, actual_margin, min_margin, total
    )

    # ── Assemble price_logic audit trail ─────────────────────────────────
    full_logic = {
        "1_cost_build_up":   cost_logic,
        "2_selling_price":   price_logic,
        "3_discount":        disc_logic,
        "4_gst_and_totals":  total_logic,
    }

    result = PricingResult(
        inquiry_id=extraction.inquiry_id,
        product_code=product_code,
        product_name=matched.name,
        quantity_mt=qty_mt,
        rm_cost_per_mt=rm_cost,
        overhead_per_mt=round(overhead, 2),
        transport_per_mt=transport,
        total_cost_per_mt=round(total_cost, 2),
        list_price_per_mt=list_price,
        floor_price_per_mt=floor_price,
        suggested_price_per_mt=suggested_price,
        customer_type=customer_type,
        discount_band_label=band_label,
        max_discount_pct=max_discount,
        approval_limit_pct=approval_limit,
        applied_discount_pct=applied_discount,
        discounted_price_per_mt=discounted_price,
        min_margin_pct=min_margin,
        target_margin_pct=target_margin,
        actual_margin_pct=actual_margin,
        gst_rate_pct=gst_rate,
        gst_per_mt=gst_per_mt,
        final_price_per_mt_ex_gst=discounted_price,
        final_price_per_mt_inc_gst=price_inc_gst,
        subtotal_ex_gst=subtotal,
        gst_amount=gst_amount,
        total_invoice_value=total,
        requires_human_approval=needs_approval,
        approval_reasons=approval_reasons,
        can_proceed_without_approval=not needs_approval,
        price_logic=full_logic,
        pricing_possible=True,
    )

    result.explanation = _generate_explanation(
        result, client, rag_context
    )
    return result


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cat_mod  = import_module("03_catalog")
    inv_mod2 = import_module("07_inventory")
    MatchType      = req_mod.MatchType
    FulfillmentType = fe_mod.FulfillmentType
    Priority        = qual_mod.Priority
    LeadTemperature = qual_mod.LeadTemperature

    docs = pd_mod.load_pricing_documents()
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
             if os.environ.get("GEMINI_API_KEY") else None

    test_cases = [
        # (inq_id, product_code, name, category, qty, zone, cust_type, label)
        ("INQ-001","MSB-001","MS Billet","Steel Billet",      "500 MT",  "North", "existing","Existing, 500MT — comfortable"),
        ("INQ-002","MSB-001","MS Billet","Steel Billet",      "1200 MT", "West",  "existing","Existing, 1200MT — large order flag"),
        ("INQ-003","PIP-001","MS Pipe",  "Steel Pipe",        "200 MT",  "West",  "new",     "New customer — lower discount band"),
        ("INQ-004", None,    "ASTM A36", "Custom",            "100 MT",  "South", "new",     "Custom product — cannot price"),
    ]

    for inq_id, pcode, pname, pcat, qty, zone, ctype, label in test_cases:
        product = cat_mod.CatalogProduct(
            product_code=pcode, name=pname,
            category=pcat, unit="MT"
        ) if pcode else None

        extraction = InquiryExtraction(
            inquiry_id=inq_id, product_requested=pname,
            quantity=qty, delivery_location="Ludhiana",
            extraction_confidence=0.9,
        )
        requirement = RequirementSummary(
            inquiry_id=inq_id,
            match_type=MatchType.CUSTOM if not pcode else MatchType.EXACT,
            matched_product=product, similarity_score=0.9 if pcode else 0.3,
            summary_text="test",
        )
        qualification = QualificationResult(
            inquiry_id=inq_id, company_name="Test Co",
            customer_type=ctype, score=75, score_breakdown={},
            temperature=LeadTemperature.HOT, priority=Priority.P1,
            rationale="test",
        )
        feasibility = FeasibilityResult(
            inquiry_id=inq_id,
            fulfillment_type=FulfillmentType.FROM_STOCK,
            delivery_zone=zone, location_found=True,
        )

        result = compute_pricing(extraction, requirement, qualification, feasibility, docs, client)

        print(f"\n{'='*65}")
        print(f"[{label}]")
        if not result.pricing_possible:
            print(f"  ✗ Pricing not possible — {result.approval_reasons}")
            continue

        print(f"  Product        : {result.product_name}  |  Qty: {result.quantity_mt} MT")
        print(f"\n  ── Cost Build-up ───────────────────────────────────────")
        print(f"  RM cost        : ₹{result.rm_cost_per_mt:>10,.2f}/MT")
        print(f"  Overhead       : ₹{result.overhead_per_mt:>10,.2f}/MT")
        print(f"  Transport      : ₹{result.transport_per_mt:>10,.2f}/MT  ({zone} zone)")
        print(f"  Total cost     : ₹{result.total_cost_per_mt:>10,.2f}/MT")
        print(f"\n  ── Selling Price ───────────────────────────────────────")
        print(f"  List price     : ₹{result.list_price_per_mt:>10,.2f}/MT")
        print(f"  Floor price    : ₹{result.floor_price_per_mt:>10,.2f}/MT  (min {result.min_margin_pct}% margin)")
        print(f"  Suggested      : ₹{result.suggested_price_per_mt:>10,.2f}/MT")
        print(f"\n  ── Discount ─────────────────────────────────────────────")
        print(f"  Band           : {result.discount_band_label}")
        print(f"  Applied        : {result.applied_discount_pct:.1f}%  (limit: {result.approval_limit_pct:.1f}%,  max: {result.max_discount_pct:.1f}%)")
        print(f"  Discounted     : ₹{result.discounted_price_per_mt:>10,.2f}/MT  (margin: {result.actual_margin_pct:.1f}%)")
        print(f"\n  ── GST & Invoice ────────────────────────────────────────")
        print(f"  Ex-GST/MT      : ₹{result.final_price_per_mt_ex_gst:>10,.2f}")
        print(f"  GST ({result.gst_rate_pct:.0f}%)/MT   : ₹{result.gst_per_mt:>10,.2f}")
        print(f"  Inc-GST/MT     : ₹{result.final_price_per_mt_inc_gst:>10,.2f}")
        print(f"  Subtotal       : ₹{result.subtotal_ex_gst:>12,.2f}")
        print(f"  GST total      : ₹{result.gst_amount:>12,.2f}")
        print(f"  TOTAL INVOICE  : ₹{result.total_invoice_value:>12,.2f}")
        print(f"\n  ── Approval ─────────────────────────────────────────────")
        print(f"  Needs approval : {result.requires_human_approval}")
        if result.approval_reasons:
            for r in result.approval_reasons:
                print(f"    → {r}")
        print(f"\n  Explanation:\n  {result.explanation}")
