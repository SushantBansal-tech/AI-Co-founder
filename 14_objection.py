"""
Sub-problem: Objection Detector + Negotiation Context

Responsibilities:
  1. Classify customer reply into one of 8 objection types (Gemini structured output)
  2. Extract key concern and any numbers mentioned (price, days) from the reply
  3. Build NegotiationContext deterministically from PricingResult
     — remaining discount headroom, floor price, what can be offered
  4. Generate a concrete negotiation suggestion (Gemini)

Design:
  - Detection is LLM (Gemini structured JSON) — reliable for language understanding
  - NegotiationContext is 100% deterministic math from PricingResult
  - Suggestion uses LLM but has a deterministic fallback per objection type

Run:
    GEMINI_API_KEY=xxx python 14_objection_detector.py
    (works without key — uses deterministic fallback suggestions)
"""

import os
import sys
import json
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel, Field
from google import genai

sys.path.insert(0, os.path.dirname(__file__))
pe_mod = import_module("10_pricing_agent")
PricingResult = pe_mod.PricingResult


# ── Objection taxonomy ────────────────────────────────────────────────────

class ObjectionType(str, Enum):
    PRICE_TOO_HIGH        = "price_too_high"
    DELIVERY_DELAY        = "delivery_delay"
    COMPETITOR_QUOTE      = "competitor_quote"
    PAYMENT_TERMS         = "payment_terms"
    SPECIFICATION_MISMATCH = "specification_mismatch"
    NO_DECISION_YET       = "no_decision_yet"
    POSITIVE_INTEREST     = "positive_interest"   # buying signal, no real objection
    NO_OBJECTION          = "no_objection"         # neutral / informational reply


# ── Output models ─────────────────────────────────────────────────────────

class ObjectionAnalysis(BaseModel):
    objection_type: ObjectionType
    confidence: float = Field(ge=0.0, le=1.0)
    key_concern: str                          # one-line plain-English summary
    customer_price_mentioned: Optional[float] = None  # if customer quoted a price
    customer_days_mentioned: Optional[int]   = None   # if customer mentioned a deadline
    verbatim_signal: Optional[str]           = None   # the phrase that triggered detection
    requires_human_escalation: bool          = False  # flag for sales manager


class NegotiationContext(BaseModel):
    """
    Built deterministically from PricingResult.
    Gives the sales team and LLM the exact numbers to negotiate with.
    """
    current_price_per_mt: float
    floor_price_per_mt: float
    applied_discount_pct: float
    max_discount_pct: float
    remaining_discount_pct: float           # how much more discount is possible
    best_possible_price_per_mt: float       # price at max_discount_pct
    min_acceptable_price_per_mt: float      # floor price — absolute minimum
    current_margin_pct: float
    min_margin_pct: float
    can_offer_more_discount: bool
    recommended_counter_offer_per_mt: float # midpoint between current and floor
    note: str


class NegotiationSuggestion(BaseModel):
    objection_type: ObjectionType
    context: NegotiationContext
    suggested_response: str         # what the sales team should say/write
    concession_offer: str           # specific offer to make
    what_not_to_concede: str        # guardrails for the sales team
    escalate_to_human: bool


# ── Objection detection prompt ────────────────────────────────────────────

DETECT_PROMPT = """
You are analyzing a customer's reply to an industrial B2B sales quotation.

Customer reply:
---
{reply_text}
---

Classify the SINGLE most important objection or signal in this reply.
If the customer mentions a specific price, extract it as a number only (no currency symbol).
If they mention a timeline or deadline in days, extract it as an integer.

Respond ONLY with valid JSON matching this schema exactly:
{{
  "objection_type": "<one of: price_too_high | delivery_delay | competitor_quote | payment_terms | specification_mismatch | no_decision_yet | positive_interest | no_objection>",
  "confidence": <0.0 to 1.0>,
  "key_concern": "<one sentence summary of the core concern>",
  "customer_price_mentioned": <number or null>,
  "customer_days_mentioned": <integer or null>,
  "verbatim_signal": "<the exact phrase from their reply that signals the objection, or null>",
  "requires_human_escalation": <true if legal/contract/large-volume issue, else false>
}}
"""


def detect_objection(
    reply_text: str,
    client: Optional[genai.Client],
) -> ObjectionAnalysis:
    """
    Classifies customer reply into an ObjectionAnalysis.
    Falls back to NO_OBJECTION with low confidence if LLM unavailable.
    """
    if not client:
        return ObjectionAnalysis(
            objection_type=ObjectionType.NO_OBJECTION,
            confidence=0.0,
            key_concern="Objection detection skipped — no Gemini client.",
        )

    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=DETECT_PROMPT.format(reply_text=reply_text),
            config={"response_mime_type": "application/json"},
        )
        data = json.loads(response.text)
        return ObjectionAnalysis(**data)
    except Exception as e:
        return ObjectionAnalysis(
            objection_type=ObjectionType.NO_OBJECTION,
            confidence=0.0,
            key_concern=f"Detection failed: {e}",
        )


# ── Negotiation context (deterministic) ──────────────────────────────────

def build_negotiation_context(pricing: PricingResult) -> NegotiationContext:
    """
    Pure arithmetic from PricingResult — no LLM, never fails.
    """
    remaining = round(pricing.max_discount_pct - pricing.applied_discount_pct, 2)
    best_price = round(
        pricing.suggested_price_per_mt * (1 - pricing.max_discount_pct / 100), 2
    )
    # Recommended counter = midpoint between current discounted and floor
    counter = round(
        (pricing.final_price_per_mt_ex_gst + pricing.floor_price_per_mt) / 2, 2
    )
    can_offer = remaining > 0.5  # at least 0.5% headroom

    if can_offer:
        note = (
            f"₹{remaining:.1f}% discount headroom remains "
            f"(from {pricing.applied_discount_pct:.1f}% to max {pricing.max_discount_pct:.1f}%). "
            f"Best possible price: ₹{best_price:,.0f}/MT (floor: ₹{pricing.floor_price_per_mt:,.0f}/MT)."
        )
    else:
        note = (
            f"Already at or near maximum discount ({pricing.applied_discount_pct:.1f}%). "
            f"Floor price is ₹{pricing.floor_price_per_mt:,.0f}/MT — cannot go lower without approval."
        )

    return NegotiationContext(
        current_price_per_mt=pricing.final_price_per_mt_ex_gst,
        floor_price_per_mt=pricing.floor_price_per_mt,
        applied_discount_pct=pricing.applied_discount_pct,
        max_discount_pct=pricing.max_discount_pct,
        remaining_discount_pct=remaining,
        best_possible_price_per_mt=best_price,
        min_acceptable_price_per_mt=pricing.floor_price_per_mt,
        current_margin_pct=pricing.actual_margin_pct,
        min_margin_pct=pricing.min_margin_pct,
        can_offer_more_discount=can_offer,
        recommended_counter_offer_per_mt=counter,
        note=note,
    )


# ── Deterministic fallback suggestions per objection type ─────────────────

def _fallback_suggestion(
    objection: ObjectionAnalysis,
    ctx: NegotiationContext,
    product: str,
    qty: float,
) -> tuple[str, str, str]:
    """Returns (response, concession_offer, what_not_to_concede)."""

    if objection.objection_type == ObjectionType.PRICE_TOO_HIGH:
        if ctx.can_offer_more_discount:
            concession = (
                f"Offer ₹{ctx.recommended_counter_offer_per_mt:,.0f}/MT "
                f"(approx. {ctx.remaining_discount_pct/2:.1f}% additional discount). "
                f"This keeps us above our floor of ₹{ctx.floor_price_per_mt:,.0f}/MT."
            )
        else:
            concession = (
                "No further price reduction possible. Offer value-adds: "
                "extended payment terms or priority dispatch instead."
            )
        return (
            f"Thank you for your feedback on the pricing. "
            f"We understand cost is critical for your operations. "
            f"We have reviewed internally and can offer a revised price. "
            f"Our current offer of ₹{ctx.current_price_per_mt:,.0f}/MT already includes "
            f"a {ctx.applied_discount_pct:.1f}% discount.",
            concession,
            "Do not reveal the floor price. Do not offer max discount immediately — negotiate in steps.",
        )

    elif objection.objection_type == ObjectionType.DELIVERY_DELAY:
        return (
            "We understand your urgency. Let us check what we can expedite from available stock.",
            "Offer to split shipment: partial from stock within 2-3 days, balance in agreed timeline.",
            "Do not commit to an impossible deadline. Confirm with dispatch before promising.",
        )

    elif objection.objection_type == ObjectionType.COMPETITOR_QUOTE:
        return (
            "We appreciate your transparency. We would like to understand their offer better "
            "to ensure you are making a like-for-like comparison.",
            "Offer to match on payment terms or add quality cert / test reports at no extra cost. "
            "Highlight IS certification, consistent grade, and after-sales support.",
            "Do not drop price blindly to match competitor. Ask to see their quotation first.",
        )

    elif objection.objection_type == ObjectionType.PAYMENT_TERMS:
        return (
            "We value your business and want to make this work for both sides. "
            "Let us see what payment flexibility we can offer.",
            "Offer to extend net payment by 15 days for orders above ₹5L, "
            "or explore LC terms for larger volumes.",
            "Do not offer open credit without checking customer credit limit and outstanding dues.",
        )

    elif objection.objection_type == ObjectionType.SPECIFICATION_MISMATCH:
        return (
            "Thank you for clarifying. We want to ensure we supply exactly what you need. "
            "Could you share the exact specification or drawing?",
            "Offer a free technical consultation to align specification before revised quotation.",
            "Do not commit to a non-standard spec without checking with production/engineering first.",
        )

    elif objection.objection_type == ObjectionType.NO_DECISION_YET:
        return (
            "Understood — we know procurement decisions take time. "
            "We just wanted to ensure the quotation is still under consideration.",
            "Offer to extend quotation validity by 15 days if they need more time.",
            "Do not pressure the customer. Keep the door open.",
        )

    elif objection.objection_type == ObjectionType.POSITIVE_INTEREST:
        return (
            "Thank you for the positive response! We look forward to your confirmation. "
            "Please share the Purchase Order at your earliest convenience.",
            "Offer to reserve material for 48 hours to help them move fast.",
            "Do not offer further discounts unprompted when they are already interested.",
        )

    else:  # NO_OBJECTION
        return (
            "Thank you for your reply. Please let us know if you have any questions "
            "about the quotation or need any clarification.",
            "No specific concession needed at this stage.",
            "Wait for customer's next move before offering anything further.",
        )


# ── LLM-powered negotiation suggestion ───────────────────────────────────

NEGOTIATION_PROMPT = """
You are a senior B2B industrial sales consultant.

Customer objection: {objection_type}
Key concern: {key_concern}
{customer_price_line}

Our quotation:
  Product        : {product} | Qty: {qty} MT
  Current price  : ₹{current_price:,.0f}/MT ex-GST (after {applied_disc:.1f}% discount)
  Floor price    : ₹{floor_price:,.0f}/MT (absolute minimum, {min_margin:.0f}% margin)
  Max discount   : {max_disc:.1f}% | Remaining headroom: {remaining:.1f}%
  Best price possible: ₹{best_price:,.0f}/MT

Write a concise negotiation playbook (4–5 sentences total):
1. What to say to the customer (2 sentences — empathetic, factual)
2. Specific concession to offer (1 sentence with exact numbers)
3. What NOT to concede immediately (1 sentence — guardrail)

Be direct. No fluff. Use Indian B2B sales context.
"""


def suggest_negotiation(
    objection: ObjectionAnalysis,
    pricing: PricingResult,
    client: Optional[genai.Client],
) -> NegotiationSuggestion:
    ctx = build_negotiation_context(pricing)

    fb_response, fb_concession, fb_guardrail = _fallback_suggestion(
        objection, ctx,
        pricing.product_name or "product",
        pricing.quantity_mt,
    )

    if client and objection.objection_type not in (
        ObjectionType.NO_OBJECTION, ObjectionType.POSITIVE_INTEREST
    ):
        cust_price_line = ""
        if objection.customer_price_mentioned:
            gap = pricing.final_price_per_mt_ex_gst - objection.customer_price_mentioned
            cust_price_line = (
                f"Customer expects: ₹{objection.customer_price_mentioned:,.0f}/MT "
                f"(gap of ₹{gap:,.0f}/MT from our current offer)"
            )
        try:
            prompt = NEGOTIATION_PROMPT.format(
                objection_type=objection.objection_type.value,
                key_concern=objection.key_concern,
                customer_price_line=cust_price_line,
                product=pricing.product_name or "product",
                qty=pricing.quantity_mt,
                current_price=pricing.final_price_per_mt_ex_gst,
                applied_disc=pricing.applied_discount_pct,
                floor_price=pricing.floor_price_per_mt,
                min_margin=pricing.min_margin_pct,
                max_disc=pricing.max_discount_pct,
                remaining=ctx.remaining_discount_pct,
                best_price=ctx.best_possible_price_per_mt,
            )
            resp = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
            suggested = resp.text.strip()
        except Exception:
            suggested = fb_response
    else:
        suggested = fb_response

    return NegotiationSuggestion(
        objection_type=objection.objection_type,
        context=ctx,
        suggested_response=suggested,
        concession_offer=fb_concession,
        what_not_to_concede=fb_guardrail,
        escalate_to_human=objection.requires_human_escalation or not ctx.can_offer_more_discount,
    )


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
             if os.environ.get("GEMINI_API_KEY") else None

    # Mock pricing result (from earlier stage output)
    pricing = PricingResult(
        inquiry_id="INQ-001",
        product_code="MSB-001",
        product_name="MS Billet IS2062",
        quantity_mt=500.0,
        rm_cost_per_mt=11500.0,
        overhead_per_mt=920.0,
        transport_per_mt=450.0,
        total_cost_per_mt=12870.0,
        list_price_per_mt=14500.0,
        floor_price_per_mt=13989.0,
        suggested_price_per_mt=14500.0,
        customer_type="existing",
        applied_discount_pct=5.0,
        max_discount_pct=12.0,
        approval_limit_pct=8.0,
        discounted_price_per_mt=13775.0,
        min_margin_pct=8.0,
        target_margin_pct=15.0,
        actual_margin_pct=8.5,
        gst_rate_pct=18.0,
        gst_per_mt=2479.5,
        final_price_per_mt_ex_gst=13775.0,
        final_price_per_mt_inc_gst=16254.5,
        subtotal_ex_gst=6887500.0,
        gst_amount=1239750.0,
        total_invoice_value=8127250.0,
        pricing_possible=True,
    )

    test_replies = [
        ("Your price is too high. Other suppliers are offering ₹13,000/MT. "
         "Can you match that rate?",
         "Price too high with competitor mention"),

        ("We need delivery within 7 days. Your 14-day timeline won't work for us.",
         "Delivery delay objection"),

        ("We need 60-day credit terms instead of 30 days.",
         "Payment terms objection"),

        ("Looks good, we'll get back to you next week after management review.",
         "Positive interest / no decision yet"),
    ]

    for reply, label in test_replies:
        print(f"\n{'='*62}")
        print(f"[{label}]")
        print(f"Customer: \"{reply[:70]}...\"" if len(reply) > 70 else f"Customer: \"{reply}\"")

        objection = detect_objection(reply, client)
        print(f"\nObjection type : {objection.objection_type.value}")
        print(f"Confidence     : {objection.confidence:.0%}")
        print(f"Key concern    : {objection.key_concern}")
        if objection.customer_price_mentioned:
            print(f"Price mentioned: ₹{objection.customer_price_mentioned:,.0f}/MT")
        if objection.verbatim_signal:
            print(f"Signal phrase  : \"{objection.verbatim_signal}\"")

        suggestion = suggest_negotiation(objection, pricing, client)
        ctx = suggestion.context
        print(f"\nNegotiation context:")
        print(f"  Current offer : ₹{ctx.current_price_per_mt:,.0f}/MT")
        print(f"  Floor price   : ₹{ctx.floor_price_per_mt:,.0f}/MT")
        print(f"  Remaining disc: {ctx.remaining_discount_pct:.1f}%  →  "
              f"best price ₹{ctx.best_possible_price_per_mt:,.0f}/MT")
        print(f"  Counter offer : ₹{ctx.recommended_counter_offer_per_mt:,.0f}/MT")
        print(f"\nSuggested response:\n  {suggestion.suggested_response[:200]}")
        print(f"\nConcession to offer:\n  {suggestion.concession_offer}")
        print(f"\nDO NOT concede:\n  {suggestion.what_not_to_concede}")
        print(f"\nEscalate to human: {suggestion.escalate_to_human}")