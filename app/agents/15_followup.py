"""
Sub-problem: Follow-up Composer + Cycle Runner

Responsibilities:
  1. generate_reminder()  — channel-aware reminder message per tone/attempt
  2. generate_objection_response() — tailored response wrapping NegotiationSuggestion
  3. run_followup_cycle() — full daily cycle:
       get_due_followups → compose → mock-send → log → return summary

Tone matrix:
  attempt 1  gentle   → "Just checking in, hope you received our quotation"
  attempt 2  moderate → "Wanted to follow up — validity is running"
  attempt 3  urgent   → "Our quotation expires soon, please confirm"
  attempt 4  final    → "Final notice — quotation expires in 5 days"

Design:
  - LLM used for all message bodies (Gemini) with deterministic fallback per tone
  - Deterministic fallbacks are production-safe, not placeholder text
  - Full cycle runner is what the scheduler (cron / Celery beat) will call daily

Run:
    GEMINI_API_KEY=xxx python 15_followup_composer.py
"""

import os
import sys
import json
import asyncio
from datetime import datetime
from typing import Optional
from importlib import import_module

from pydantic import BaseModel
from google import genai
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

sys.path.insert(0, os.path.dirname(__file__))
ia    = import_module("01_Inquiry")
ft    = import_module("13_followup")
od    = import_module("14_objection")
qb    = import_module("11_quotation")

FollowUpType         = ft.FollowUpType
FollowUpStatus       = ft.FollowUpStatus
ScheduleItem         = ft.ScheduleItem
create_followup_record = ft.create_followup_record
get_due_followups    = ft.get_due_followups
ObjectionAnalysis    = od.ObjectionAnalysis
ObjectionType        = od.ObjectionType
NegotiationSuggestion = od.NegotiationSuggestion
detect_objection     = od.detect_objection
QuotationDraft       = qb.QuotationDraft


# ── Dispatch result ───────────────────────────────────────────────────────

class FollowUpDispatchResult(BaseModel):
    quotation_number: str
    buyer_company: str
    channel: str
    recipient: str
    attempt: int
    tone: str
    message_text: str
    sent: bool


# ── TONE-BASED REMINDER TEMPLATES (deterministic fallback) ────────────────

def _reminder_fallback(
    draft: QuotationDraft,
    schedule: ScheduleItem,
    days_remaining: int,
) -> str:
    q   = draft.quotation_number
    co  = draft.buyer_company
    name = draft.buyer_contact or "Sir/Madam"
    val = f"₹{draft.total_inc_gst:,.0f}"
    exp = draft.valid_until

    if schedule.tone == "gentle":
        return (
            f"Dear {name},\n\n"
            f"Hope you are doing well.\n\n"
            f"This is a gentle reminder about our Quotation {q} submitted to {co} "
            f"for ₹{draft.subtotal_ex_gst:,.0f} + GST. "
            f"We hope you had a chance to review it.\n\n"
            f"Please feel free to reach out if you have any questions or need clarification "
            f"on the pricing, delivery timeline, or payment terms.\n\n"
            f"Looking forward to your valued confirmation.\n\n"
            f"Warm regards,\n{draft.seller_name}"
        )

    elif schedule.tone == "moderate":
        return (
            f"Dear {name},\n\n"
            f"We wanted to follow up on Quotation {q} (Total: {val} incl. GST) "
            f"sent to {co}.\n\n"
            f"The quotation is valid until {exp} ({days_remaining} days remaining). "
            f"To secure the current pricing and ensure timely delivery, "
            f"we kindly request your confirmation at the earliest.\n\n"
            f"If there are any concerns or queries, we are happy to discuss.\n\n"
            f"Regards,\n{draft.seller_name}"
        )

    elif schedule.tone == "urgent":
        return (
            f"Dear {name},\n\n"
            f"This is an important follow-up regarding Quotation {q} for {co}.\n\n"
            f"Our quotation is valid until {exp} — only {days_remaining} days remaining. "
            f"After expiry, revised pricing will apply based on current market rates.\n\n"
            f"If you have already decided or have any objections, please do let us know "
            f"so we can address them promptly. We value your business and want to ensure "
            f"a smooth process.\n\n"
            f"Please confirm or advise at the earliest.\n\n"
            f"Regards,\n{draft.seller_name}"
        )

    else:  # final
        return (
            f"Dear {name},\n\n"
            f"FINAL NOTICE — Quotation {q} expires on {exp} ({days_remaining} days).\n\n"
            f"After this date, we cannot guarantee the quoted price of {val} "
            f"or the availability of stock. "
            f"Please confirm your purchase order or request an extension before the validity date.\n\n"
            f"We look forward to serving {co} and hope to receive your confirmation soon.\n\n"
            f"Regards,\n{draft.seller_name}"
        )


# ── REMINDER PROMPT (LLM version) ─────────────────────────────────────────

REMINDER_PROMPT = """
Write a {tone} follow-up {channel} message for an industrial B2B sales team.

Context:
  Quotation No : {q_no}
  Customer     : {buyer_name} at {company}
  Product      : {product}
  Total value  : ₹{total:,.0f} (incl. GST)
  Valid until  : {valid_until} ({days_remaining} days remaining)
  Attempt      : {attempt} of 4

Tone guidance:
  gentle   → warm, no pressure, just checking in
  moderate → professional, mild urgency about validity
  urgent   → clear urgency, highlight expiry risk
  final    → last chance, formal, factual

Rules:
  - {channel_rule}
  - Do not mention competitors or internal pricing details
  - End with a clear call-to-action

Write only the message body, no subject line.
"""

CHANNEL_RULES = {
    "whatsapp": "Keep under 5 lines, conversational, use the customer's first name",
    "email":    "Professional format, 3–4 short paragraphs, formal salutation",
}


def generate_reminder(
    draft: QuotationDraft,
    schedule: ScheduleItem,
    channel: str,
    client: Optional[genai.Client] = None,
) -> str:
    from datetime import datetime as dt
    try:
        valid_parts = draft.valid_until.split("-")
        exp_date = dt.strptime(draft.valid_until, "%d-%m-%Y")
        days_remaining = max(0, (exp_date - dt.utcnow()).days)
    except Exception:
        days_remaining = 30 - schedule.days_after

    fallback = _reminder_fallback(draft, schedule, days_remaining)
    if not client:
        return fallback

    product_name = draft.line_items[0].description if draft.line_items else "your requirement"
    buyer_first = (draft.buyer_contact or "Sir/Madam").split()[0]

    try:
        prompt = REMINDER_PROMPT.format(
            tone=schedule.tone,
            channel=channel,
            q_no=draft.quotation_number,
            buyer_name=buyer_first,
            company=draft.buyer_company,
            product=product_name,
            total=draft.total_inc_gst,
            valid_until=draft.valid_until,
            days_remaining=days_remaining,
            attempt=schedule.attempt,
            channel_rule=CHANNEL_RULES.get(channel, CHANNEL_RULES["email"]),
        )
        resp = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
        return resp.text.strip()
    except Exception:
        return fallback


# ── OBJECTION RESPONSE PROMPT ─────────────────────────────────────────────

OBJECTION_RESPONSE_PROMPT = """
Write a {channel} response to a customer's objection for an Indian industrial B2B company.

Quotation: {q_no} | Customer: {buyer_name} at {company}
Product: {product} | Current price: ₹{current_price:,.0f}/MT (ex-GST)

Customer objection: {objection_type}
Their concern: {key_concern}
{price_gap_line}

Negotiation playbook to follow:
{suggested_response}

Specific concession approved: {concession}

Rules:
  - {channel_rule}
  - Sound helpful, not defensive
  - Make the concession sound like a special gesture
  - End with a specific next step (call, revised quote, etc.)

Write only the message body.
"""


def generate_objection_response(
    draft: QuotationDraft,
    objection: ObjectionAnalysis,
    suggestion: NegotiationSuggestion,
    channel: str,
    client: Optional[genai.Client] = None,
) -> str:
    product_name = draft.line_items[0].description if draft.line_items else "material"
    ctx = suggestion.context

    price_gap_line = ""
    if objection.customer_price_mentioned:
        gap = ctx.current_price_per_mt - objection.customer_price_mentioned
        price_gap_line = (
            f"Customer mentioned ₹{objection.customer_price_mentioned:,.0f}/MT "
            f"(gap: ₹{gap:,.0f}/MT from our offer)"
        )

    fallback = (
        f"Dear {draft.buyer_contact or 'Sir/Madam'},\n\n"
        f"Thank you for your reply regarding Quotation {draft.quotation_number}.\n\n"
        f"{suggestion.suggested_response}\n\n"
        f"{suggestion.concession_offer}\n\n"
        f"Please let us know if you would like to discuss further. "
        f"We are happy to get on a call at your convenience.\n\n"
        f"Regards,\n{draft.seller_name}"
    )

    if not client:
        return fallback

    try:
        prompt = OBJECTION_RESPONSE_PROMPT.format(
            channel=channel,
            q_no=draft.quotation_number,
            buyer_name=(draft.buyer_contact or "Sir/Madam").split()[0],
            company=draft.buyer_company,
            product=product_name,
            current_price=ctx.current_price_per_mt,
            objection_type=objection.objection_type.value.replace("_", " ").title(),
            key_concern=objection.key_concern,
            price_gap_line=price_gap_line,
            suggested_response=suggestion.suggested_response[:300],
            concession=suggestion.concession_offer,
            channel_rule=CHANNEL_RULES.get(channel, CHANNEL_RULES["email"]),
        )
        resp = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
        return resp.text.strip()
    except Exception:
        return fallback


# ── Mock dispatcher (same pattern as 12_quotation_renderer) ──────────────

def _mock_send(channel: str, recipient: str, message: str) -> bool:
    print(f"  [MOCK {channel.upper()}] → {recipient}")
    print(f"  Preview: {message[:120].replace(chr(10),' ')}...")
    return True


# ── FULL DAILY CYCLE ──────────────────────────────────────────────────────

async def run_followup_cycle(
    session: AsyncSession,
    client: Optional[genai.Client] = None,
) -> list[FollowUpDispatchResult]:
    """
    Called once per day by scheduler.
    1. Find all quotations that have a follow-up due today
    2. Load quotation draft
    3. Compose message
    4. Mock-send via channel
    5. Log to DB
    Returns list of FollowUpDispatchResult for audit.
    """
    due_list = await get_due_followups(session)
    results  = []

    if not due_list:
        print("No follow-ups due today.")
        return results

    for item in due_list:
        schedule: ScheduleItem   = item["schedule"]
        quotation_number: str    = item["quotation_number"]
        buyer_company: str       = item["buyer_company"]
        draft_json: str          = item.get("draft_json", "{}")

        # Rebuild QuotationDraft from stored JSON
        try:
            draft_data = json.loads(draft_json)
            draft = QuotationDraft(**draft_data)
        except Exception:
            print(f"  Skipping {quotation_number} — draft JSON parse error")
            continue

        # Compose message
        channel   = "email"    # default; real impl checks customer preference
        recipient = draft.seller_email  # in prod: draft.buyer_email

        message = generate_reminder(draft, schedule, channel, client)

        # Mock send
        print(f"\nSending {schedule.label} for {quotation_number} → {buyer_company}")
        sent = _mock_send(channel, recipient, message)

        # Log to DB
        if sent:
            await create_followup_record(
                session=session,
                quotation_id=item["quotation_id"],
                quotation_number=quotation_number,
                inquiry_id=item.get("inquiry_id", ""),
                buyer_company=buyer_company,
                channel=channel,
                recipient=recipient,
                attempt=schedule.attempt,
                followup_type=schedule.followup_type,
                tone=schedule.tone,
                message_text=message,
            )

        results.append(FollowUpDispatchResult(
            quotation_number=quotation_number,
            buyer_company=buyer_company,
            channel=channel,
            recipient=recipient,
            attempt=schedule.attempt,
            tone=schedule.tone,
            message_text=message,
            sent=sent,
        ))

    return results


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
             if os.environ.get("GEMINI_API_KEY") else None

    # ── Test 1: Reminder message for each tone ────────────────────────
    print("=" * 62)
    print("TEST 1 — Reminder messages per tone (fallback, no API key needed)")

    sample_draft = QuotationDraft(
        quotation_number="QT-2025-A1B2",
        inquiry_id="INQ-001",
        valid_until="30-06-2025",
        buyer_company="Apex Steel Pvt Ltd",
        buyer_contact="Ramesh Kumar",
        buyer_delivery_location="Ludhiana",
        seller_name="IndusSteel Trading Pvt. Ltd.",
        seller_email="sales@indussteel.in",
        subtotal_ex_gst=6887500.0,
        total_gst_amount=1239750.0,
        total_inc_gst=8127250.0,
        payment_terms_text="20% advance, balance net 45 days",
        delivery_timeline="Ex-stock, dispatch in 2-3 days.",
    )

    for sched_item in ft.FOLLOW_UP_SCHEDULE:
        msg = generate_reminder(sample_draft, sched_item, "email", client)
        print(f"\n[Attempt {sched_item.attempt} — {sched_item.tone.upper()}]")
        print(msg[:300] + "..." if len(msg) > 300 else msg)

    # ── Test 2: Objection response ────────────────────────────────────
    print("\n" + "=" * 62)
    print("TEST 2 — Objection response (price too high)")

    objection = ObjectionAnalysis(
        objection_type=ObjectionType.PRICE_TOO_HIGH,
        confidence=0.92,
        key_concern="Customer says price is ₹1,000/MT above their budget.",
        customer_price_mentioned=12775.0,
        verbatim_signal="your price is too high",
    )
    pe_mod = import_module("10_pricing_agent")
    pricing = pe_mod.PricingResult(
        inquiry_id="INQ-001", product_code="MSB-001",
        product_name="MS Billet IS2062", quantity_mt=500.0,
        rm_cost_per_mt=11500.0, overhead_per_mt=920.0,
        transport_per_mt=450.0, total_cost_per_mt=12870.0,
        list_price_per_mt=14500.0, floor_price_per_mt=13989.0,
        suggested_price_per_mt=14500.0, customer_type="existing",
        applied_discount_pct=5.0, max_discount_pct=12.0,
        approval_limit_pct=8.0, discounted_price_per_mt=13775.0,
        min_margin_pct=8.0, target_margin_pct=15.0, actual_margin_pct=8.5,
        gst_rate_pct=18.0, gst_per_mt=2479.5,
        final_price_per_mt_ex_gst=13775.0, final_price_per_mt_inc_gst=16254.5,
        subtotal_ex_gst=6887500.0, gst_amount=1239750.0,
        total_invoice_value=8127250.0, pricing_possible=True,
    )
    suggestion = od.suggest_negotiation(objection, pricing, client)
    response_msg = generate_objection_response(
        sample_draft, objection, suggestion, "email", client
    )
    print(f"\nObjection type    : {objection.objection_type.value}")
    print(f"Counter offer     : ₹{suggestion.context.recommended_counter_offer_per_mt:,.0f}/MT")
    print(f"Escalate to human : {suggestion.escalate_to_human}")
    print(f"\nResponse message preview:\n{response_msg[:400]}")