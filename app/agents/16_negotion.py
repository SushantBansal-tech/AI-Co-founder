"""
Sub-problem: Negotiation Engine

Responsibilities:
  1. Extract customer's offered price from free-text message (Gemini)
  2. Compare against: current price, floor price, approval limit, max discount
  3. Produce NegotiationDecision — ACCEPTABLE / NEEDS_APPROVAL / BELOW_FLOOR
  4. Compute counter-proposal when offer is close but not acceptable
  5. Generate a short response rationale (Gemini, with fallback)

Decision matrix (all pure math — no LLM):
  ┌─────────────────────────────────────────────────────────────┐
  │ Customer offer >= current_price         → ACCEPTABLE        │
  │ Customer offer >= approval_limit_price  → ACCEPTABLE        │
  │ Customer offer >= floor_price           → NEEDS_APPROVAL    │
  │ Customer offer <  floor_price           → BELOW_FLOOR       │
  └─────────────────────────────────────────────────────────────┘

Design rule: Every number in this file is arithmetic.
LLM touches ONLY price extraction from text and response rationale.

Run:
    GEMINI_API_KEY=xxx python 16_negotiation_engine.py
    (works without key — deterministic fallbacks used)
"""

import os
import sys
import json
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel, Field
from google import genai
from app.rag.models import AgentRAGContext

sys.path.insert(0, os.path.dirname(__file__))
pe_mod = import_module("10_pricing_agent")
pd_mod = import_module("09_pricing")
PricingResult    = pe_mod.PricingResult
PricingDocuments = pd_mod.PricingDocuments


# ── Decision enum ─────────────────────────────────────────────────────────

class NegotiationDecision(str, Enum):
    ACCEPTABLE     = "acceptable"      # within auto-approval limit → auto-proceed
    NEEDS_APPROVAL = "needs_approval"  # between approval_limit and max_discount → human
    BELOW_FLOOR    = "below_floor"     # below cost+min-margin → hard no


# ── Output models ─────────────────────────────────────────────────────────

class CounterOfferExtraction(BaseModel):
    """Result of parsing the customer's message for a price."""
    raw_message: str
    price_per_mt_found: bool
    customer_price_per_mt: Optional[float] = None
    customer_total_mentioned: Optional[float] = None   # if they said "₹65L total"
    extraction_confidence: float = Field(ge=0.0, le=1.0)
    extraction_note: str = ""


class NegotiationAnalysis(BaseModel):
    """Full picture of one negotiation round."""
    # What the customer offered
    customer_price_per_mt: float
    quantity_mt: float

    # Reference points from original pricing
    original_price_per_mt: float        # price before any counter-offer
    floor_price_per_mt: float           # absolute minimum (cost + min_margin)
    approval_limit_price_per_mt: float  # auto-approve threshold
    max_possible_price_per_mt: float    # price at max_discount

    # Computed
    implied_discount_pct: float         # discount % the customer's price implies
    gap_from_floor_per_mt: float        # positive = above floor, negative = below floor
    gap_from_approval_per_mt: float     # positive = above approval limit, neg = below

    # Decision
    decision: NegotiationDecision
    can_auto_approve: bool
    decision_reason: str

    # Counter-proposal (our recommended next offer if not accepting outright)
    counter_proposal_per_mt: Optional[float] = None
    counter_proposal_rationale: str = ""

    # Revised financials at customer's offered price
    revised_subtotal_ex_gst: float = 0.0
    revised_gst_amount: float = 0.0
    revised_total_inc_gst: float = 0.0

    # Human approval
    requires_human_approval: bool = False
    human_approval_reason: Optional[str] = None


# ── Step 1: Extract price from customer message ───────────────────────────

EXTRACT_PRICE_PROMPT = """
Extract the price per MT (metric ton) that the customer is offering or requesting
from this B2B industrial sales negotiation message.

Message:
---
{message}
---

The customer may state:
- A direct per-MT price: "we can accept ₹13,000/MT"
- A total order value: "our budget is ₹65 lakhs" (divide by quantity to get per-MT)
- A discount request: "please give 8% discount" (apply to current price)
- None of the above: they haven't mentioned a specific price

Quantity context: {quantity} MT at current price ₹{current_price:,.0f}/MT

Respond ONLY with valid JSON:
{{
  "price_per_mt_found": true_or_false,
  "customer_price_per_mt": <number or null>,
  "customer_total_mentioned": <total order value in INR or null>,
  "extraction_confidence": <0.0 to 1.0>,
  "extraction_note": "<one line explaining what was found or why nothing was found>"
}}

If they mention a discount %, compute: customer_price_per_mt = current_price * (1 - discount/100)
If they mention a total, compute: customer_price_per_mt = total / quantity
"""


def extract_counteroffer_price(
    message: str,
    pricing: PricingResult,
    client: Optional[genai.Client],
) -> CounterOfferExtraction:
    if not client:
        return CounterOfferExtraction(
            raw_message=message,
            price_per_mt_found=False,
            extraction_confidence=0.0,
            extraction_note="No Gemini client — price extraction skipped.",
        )
    try:
        prompt = EXTRACT_PRICE_PROMPT.format(
            message=message,
            quantity=pricing.quantity_mt,
            current_price=pricing.final_price_per_mt_ex_gst,
        )
        resp = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        data = json.loads(resp.text)
        return CounterOfferExtraction(raw_message=message, **data)
    except Exception as e:
        return CounterOfferExtraction(
            raw_message=message,
            price_per_mt_found=False,
            extraction_confidence=0.0,
            extraction_note=f"Extraction error: {e}",
        )


# ── Step 2: Deterministic decision ───────────────────────────────────────

def _compute_price_at_discount(list_price: float, discount_pct: float) -> float:
    return round(list_price * (1 - discount_pct / 100), 2)


def evaluate_counteroffer(
    customer_price_per_mt: float,
    pricing: PricingResult,
    rag_context: Optional[AgentRAGContext] = None,
) -> NegotiationAnalysis:
    """
    100% deterministic — no LLM.
    Uses PricingResult to establish all reference prices, then computes decision.
    """
    qty             = pricing.quantity_mt
    current_price   = pricing.final_price_per_mt_ex_gst
    floor_price     = pricing.floor_price_per_mt
    list_price      = pricing.suggested_price_per_mt
    max_disc        = pricing.max_discount_pct
    approval_limit  = pricing.approval_limit_pct
    gst_rate        = pricing.gst_rate_pct

    # Reference prices
    approval_limit_price = _compute_price_at_discount(list_price, approval_limit)
    max_possible_price   = _compute_price_at_discount(list_price, max_disc)

    # What discount % does the customer's price imply from list price?
    implied_discount = round((1 - customer_price_per_mt / list_price) * 100, 2) \
                       if list_price > 0 else 0.0

    gap_from_floor    = round(customer_price_per_mt - floor_price, 2)
    gap_from_approval = round(customer_price_per_mt - approval_limit_price, 2)

    # ── Decision logic (floor price takes absolute priority) ─────────
    if customer_price_per_mt >= current_price:
        decision     = NegotiationDecision.ACCEPTABLE
        auto_approve = True
        reason       = (
            f"Customer's price ₹{customer_price_per_mt:,.0f}/MT is at or above "
            f"our current offer ₹{current_price:,.0f}/MT — no concession needed."
        )
        counter      = None
        counter_note = ""

    elif customer_price_per_mt < floor_price:
        # Hard floor breach — discount policy is irrelevant here
        decision     = NegotiationDecision.BELOW_FLOOR
        auto_approve = False
        reason       = (
            f"Customer's price ₹{customer_price_per_mt:,.0f}/MT is below cost floor "
            f"₹{floor_price:,.0f}/MT by ₹{floor_price - customer_price_per_mt:,.0f}/MT. "
            f"Cannot accept — this would breach minimum margin of {pricing.min_margin_pct:.0f}%."
        )
        counter      = floor_price
        counter_note = (
            f"Minimum acceptable price is ₹{floor_price:,.0f}/MT. "
            "Offer this as our final price, or escalate to human for exception approval."
        )

    elif customer_price_per_mt >= approval_limit_price:
        # Above floor AND within auto-approval band
        decision     = NegotiationDecision.ACCEPTABLE
        auto_approve = True
        reason       = (
            f"Customer's price ₹{customer_price_per_mt:,.0f}/MT is above floor "
            f"(₹{floor_price:,.0f}/MT) and within auto-approval limit "
            f"(₹{approval_limit_price:,.0f}/MT at {approval_limit:.1f}% discount). "
            f"Implied discount: {implied_discount:.1f}%."
        )
        counter      = None
        counter_note = ""

    else:
        # Above floor but below auto-approval limit → needs manager sign-off
        decision     = NegotiationDecision.NEEDS_APPROVAL
        auto_approve = False
        reason       = (
            f"Customer's price ₹{customer_price_per_mt:,.0f}/MT is above floor "
            f"(₹{floor_price:,.0f}/MT) but below auto-approval limit "
            f"(₹{approval_limit_price:,.0f}/MT at {approval_limit:.1f}% discount). "
            f"Implied discount: {implied_discount:.1f}% exceeds auto-limit. "
            "Requires sales manager approval."
        )
        # Counter: midpoint between approval_limit and floor (always above floor)
        counter = round(max((approval_limit_price + floor_price) / 2, floor_price), 2)
        counter_note = (
            f"Suggest offering ₹{counter:,.0f}/MT as a counter "
            f"(midpoint between auto-limit ₹{approval_limit_price:,.0f} "
            f"and floor ₹{floor_price:,.0f}/MT) while manager approval is sought."
        )

    # Revised financials at customer's offered price
    rev_subtotal = round(customer_price_per_mt * qty, 2)
    rev_gst      = round(rev_subtotal * gst_rate / 100, 2)
    rev_total    = round(rev_subtotal + rev_gst, 2)

    human_needed = decision in (NegotiationDecision.NEEDS_APPROVAL, NegotiationDecision.BELOW_FLOOR)
    human_reason = reason if human_needed else None

    return NegotiationAnalysis(
        customer_price_per_mt=customer_price_per_mt,
        quantity_mt=qty,
        original_price_per_mt=current_price,
        floor_price_per_mt=floor_price,
        approval_limit_price_per_mt=approval_limit_price,
        max_possible_price_per_mt=max_possible_price,
        implied_discount_pct=implied_discount,
        gap_from_floor_per_mt=gap_from_floor,
        gap_from_approval_per_mt=gap_from_approval,
        decision=decision,
        can_auto_approve=auto_approve,
        decision_reason=reason,
        counter_proposal_per_mt=counter,
        counter_proposal_rationale=counter_note,
        revised_subtotal_ex_gst=rev_subtotal,
        revised_gst_amount=rev_gst,
        revised_total_inc_gst=rev_total,
        requires_human_approval=human_needed,
        human_approval_reason=human_reason,
    )


# ── Step 3: Response rationale (LLM polish on the decision) ──────────────

RATIONALE_PROMPT = """
Write a 2-3 sentence internal note for a B2B industrial sales manager explaining
a negotiation decision. Be factual and direct.

Customer offered: ₹{customer_price:,.0f}/MT (implied {implied_disc:.1f}% discount)
Our current price: ₹{current_price:,.0f}/MT | Floor: ₹{floor_price:,.0f}/MT
Decision: {decision}
Reason: {reason}
{counter_line}

Include: what decision was made, why, and the recommended next action.
No fluff.
"""


def generate_negotiation_rationale(
    analysis: NegotiationAnalysis,
    client: Optional[genai.Client],
) -> str:
    fallback = (
        f"Decision: {analysis.decision.value.upper()}. "
        f"Customer offered ₹{analysis.customer_price_per_mt:,.0f}/MT "
        f"({analysis.implied_discount_pct:.1f}% discount). "
        f"{analysis.decision_reason} "
        f"{analysis.counter_proposal_rationale}"
    ).strip()

    if not client or analysis.decision == NegotiationDecision.ACCEPTABLE:
        return fallback
    try:
        counter_line = (
            f"Recommended counter-offer: ₹{analysis.counter_proposal_per_mt:,.0f}/MT"
            if analysis.counter_proposal_per_mt else ""
        )
        prompt = RATIONALE_PROMPT.format(
            customer_price=analysis.customer_price_per_mt,
            implied_disc=analysis.implied_discount_pct,
            current_price=analysis.original_price_per_mt,
            floor_price=analysis.floor_price_per_mt,
            decision=analysis.decision.value.upper(),
            reason=analysis.decision_reason,
            counter_line=counter_line,
        )
        resp = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)
        return resp.text.strip()
    except Exception:
        return fallback


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
             if os.environ.get("GEMINI_API_KEY") else None

    pricing = PricingResult(
        inquiry_id="INQ-001", product_code="MSB-001",
        product_name="MS Billet IS2062", quantity_mt=500.0,
        rm_cost_per_mt=11500, overhead_per_mt=920, transport_per_mt=450,
        total_cost_per_mt=12870, list_price_per_mt=14500,
        floor_price_per_mt=13989, suggested_price_per_mt=14500,
        customer_type="existing",
        applied_discount_pct=5.0, max_discount_pct=12.0,
        approval_limit_pct=8.0, discounted_price_per_mt=13775,
        min_margin_pct=8.0, target_margin_pct=15.0, actual_margin_pct=8.5,
        gst_rate_pct=18.0, gst_per_mt=2479.5,
        final_price_per_mt_ex_gst=13775, final_price_per_mt_inc_gst=16254.5,
        subtotal_ex_gst=6887500, gst_amount=1239750,
        total_invoice_value=8127250, pricing_possible=True,
    )

    test_cases = [
        # (customer_price, label)
        (13800, "Within auto-approval limit (≥ ₹13,468 at 8%)"),
        (13400, "Below auto-limit, above floor → NEEDS_APPROVAL"),
        (12500, "Below floor price (₹13,989) → BELOW_FLOOR"),
        (14000, "Above current offer → ACCEPTABLE no concession"),
    ]

    for price, label in test_cases:
        print(f"\n{'='*62}")
        print(f"[{label}]")
        print(f"Customer offers: ₹{price:,.0f}/MT  (current: ₹13,775/MT  floor: ₹13,989/MT)")

        analysis = evaluate_counteroffer(price, pricing)
        rationale = generate_negotiation_rationale(analysis, client)

        print(f"\nDecision         : {analysis.decision.value.upper()}")
        print(f"Can auto-approve : {analysis.can_auto_approve}")
        print(f"Implied discount : {analysis.implied_discount_pct:.1f}%")
        print(f"Gap from floor   : ₹{analysis.gap_from_floor_per_mt:+,.0f}/MT")
        if analysis.counter_proposal_per_mt:
            print(f"Counter-proposal : ₹{analysis.counter_proposal_per_mt:,.0f}/MT")
            print(f"Counter rationale: {analysis.counter_proposal_rationale}")
        print(f"\nRevised totals (at customer's price):")
        print(f"  Subtotal ex-GST : ₹{analysis.revised_subtotal_ex_gst:>14,.2f}")
        print(f"  GST             : ₹{analysis.revised_gst_amount:>14,.2f}")
        print(f"  Total inc-GST   : ₹{analysis.revised_total_inc_gst:>14,.2f}")
        print(f"\nHuman approval  : {analysis.requires_human_approval}")
        print(f"Rationale       : {rationale}")
