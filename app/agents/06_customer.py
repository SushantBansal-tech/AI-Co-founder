"""
Sub-problem: Customer Qualification Agent

Takes a CustomerProfile (from 05_customer_lookup.py) and:
  1. Runs deterministic scoring across 5 signal groups
     (order history, payment behavior, credit health,
      win rate, recency) — no LLM needed for the score itself
  2. Classifies lead as HOT / WARM / COLD from the score
  3. Assigns priority P1 / P2 / P3
  4. Calls Gemini ONLY to write the human-readable qualification
     rationale (the score drives the decision, LLM explains it)
  5. Flags credit risk if outstanding > limit threshold
  6. Returns a QualificationResult — passed to Feasibility Agent next

Depends on:
  inquiry_agent.py          → InquiryExtraction, log_action
  05_customer_lookup.py     → CustomerProfile, CustomerType, PaymentBehavior

Run:
    python 06_customer_qualification.py
"""

import os
import sys
import asyncio
import json
from enum import Enum
from typing import Optional
from importlib import import_module
from dataclasses import dataclass, field

from pydantic import BaseModel
from google import genai
from app.rag.models import AgentRAGContext

sys.path.insert(0, os.path.dirname(__file__))
ia      = import_module("01_Inquiry")  # for Base, log_action, InquiryExtraction
lookup  = import_module("05_customer_qual")

InquiryExtraction = ia.InquiryExtraction
log_action        = ia.log_action
CustomerProfile   = lookup.CustomerProfile
CustomerType      = lookup.CustomerType
PaymentBehavior   = lookup.PaymentBehavior


# ---------------------------------------------------------------------------
# Output enums
# ---------------------------------------------------------------------------

class LeadTemperature(str, Enum):
    HOT  = "hot"    # score >= 70  — fast-track, senior sales
    WARM = "warm"   # score 40-69  — standard pipeline
    COLD = "cold"   # score < 40   — low effort until signals improve


class Priority(str, Enum):
    P1 = "P1"   # respond within 2 hours
    P2 = "P2"   # respond within 1 business day
    P3 = "P3"   # respond within 3 business days


# ---------------------------------------------------------------------------
# Scoring engine — fully deterministic, no LLM
# Each group contributes max points shown.  Total max = 100.
# ---------------------------------------------------------------------------

@dataclass
class ScoreBreakdown:
    order_history_score:   int = 0   # max 25 — how much they've bought before
    payment_score:         int = 0   # max 30 — payment behavior + delay days
    credit_health_score:   int = 0   # max 20 — credit utilization
    win_rate_score:        int = 0   # max 15 — how often our quotes convert
    recency_score:         int = 0   # max 10 — new customer bonus
    total:                 int = field(init=False)
    reasons:               list[str] = field(default_factory=list)

    def __post_init__(self):
        self.total = (self.order_history_score + self.payment_score +
                      self.credit_health_score + self.win_rate_score +
                      self.recency_score)


def _score_order_history(profile: CustomerProfile) -> tuple[int, list[str]]:
    """Max 25 points."""
    reasons = []
    if profile.customer_type == CustomerType.NEW:
        reasons.append("New customer — no order history.")
        return 10, reasons   # new customers get a base 10 (opportunity)

    val = profile.total_order_value
    if val >= 10_000_000:
        pts, r = 25, f"High-value customer (₹{val/1e6:.1f}L total orders)."
    elif val >= 2_000_000:
        pts, r = 18, f"Mid-value customer (₹{val/1e6:.1f}L total orders)."
    elif val >= 500_000:
        pts, r = 10, f"Low-value customer (₹{val/1e6:.1f}L total orders)."
    else:
        pts, r = 4,  f"Very low total order value (₹{val:,.0f})."
    reasons.append(r)
    return pts, reasons


def _score_payment(profile: CustomerProfile) -> tuple[int, list[str]]:
    """Max 30 points."""
    reasons = []
    if profile.customer_type == CustomerType.NEW:
        reasons.append("No payment history — neutral score.")
        return 15, reasons   # neutral for new

    behavior_pts = {
        PaymentBehavior.EXCELLENT: 30,
        PaymentBehavior.GOOD:      24,
        PaymentBehavior.AVERAGE:   14,
        PaymentBehavior.POOR:       4,
        PaymentBehavior.UNKNOWN:   15,
    }
    pts = behavior_pts[profile.payment_behavior]
    reasons.append(f"Payment behavior: {profile.payment_behavior.value} "
                   f"(avg delay {profile.avg_delay_days:.0f} days).")

    # Penalise for high average delay even if behavior tag looks ok
    if profile.avg_delay_days > 30:
        pts = max(0, pts - 10)
        reasons.append("Penalty: avg payment delay > 30 days.")
    elif profile.avg_delay_days > 15:
        pts = max(0, pts - 5)
        reasons.append("Penalty: avg payment delay > 15 days.")

    return pts, reasons


def _score_credit_health(profile: CustomerProfile) -> tuple[int, list[str]]:
    """Max 20 points."""
    reasons = []
    if profile.customer_type == CustomerType.NEW or profile.credit_limit == 0:
        reasons.append("No credit limit set — neutral.")
        return 10, reasons

    util = profile.credit_utilization_pct
    if util <= 30:
        pts, r = 20, f"Low credit utilization ({util:.0f}%) — healthy."
    elif util <= 60:
        pts, r = 14, f"Moderate credit utilization ({util:.0f}%)."
    elif util <= 85:
        pts, r = 6,  f"High credit utilization ({util:.0f}%) — caution."
    else:
        pts, r = 0,  f"Credit limit nearly exhausted ({util:.0f}%) — risk."
    reasons.append(r)
    return pts, reasons


def _score_win_rate(profile: CustomerProfile) -> tuple[int, list[str]]:
    """Max 15 points — how often our quotes become orders."""
    reasons = []
    if profile.customer_type == CustomerType.NEW:
        reasons.append("No quotation history — new opportunity.")
        return 8, reasons

    total_quotes = profile.won_quotations + profile.lost_quotations
    if total_quotes == 0:
        return 8, ["No closed quotations yet."]

    wr = profile.win_rate_pct
    if wr >= 70:
        pts, r = 15, f"Strong win rate ({wr:.0f}%) — high conversion."
    elif wr >= 40:
        pts, r = 10, f"Moderate win rate ({wr:.0f}%)."
    else:
        pts, r = 4,  f"Low win rate ({wr:.0f}%) — frequent losses."
    reasons.append(r)
    return pts, reasons


def _score_recency(profile: CustomerProfile) -> tuple[int, list[str]]:
    """Max 10 points — new customers get a flat bonus as an opportunity signal."""
    if profile.customer_type == CustomerType.NEW:
        return 10, ["New customer — opportunity bonus."]
    return 5, ["Existing customer — standard recency score."]


def compute_score(profile: CustomerProfile) -> ScoreBreakdown:
    oh_pts,  oh_r  = _score_order_history(profile)
    pay_pts, pay_r = _score_payment(profile)
    cr_pts,  cr_r  = _score_credit_health(profile)
    wr_pts,  wr_r  = _score_win_rate(profile)
    re_pts,  re_r  = _score_recency(profile)

    bd = ScoreBreakdown(
        order_history_score = oh_pts,
        payment_score       = pay_pts,
        credit_health_score = cr_pts,
        win_rate_score      = wr_pts,
        recency_score       = re_pts,
        reasons             = oh_r + pay_r + cr_r + wr_r + re_r,
    )
    return bd


# ---------------------------------------------------------------------------
# Classify from score
# ---------------------------------------------------------------------------

def classify_temperature(score: int) -> LeadTemperature:
    if score >= 70:
        return LeadTemperature.HOT
    elif score >= 40:
        return LeadTemperature.WARM
    else:
        return LeadTemperature.COLD


def assign_priority(temp: LeadTemperature, profile: CustomerProfile) -> Priority:
    """
    Priority can be upgraded beyond temperature if the order is very large
    or the customer has a long history.
    """
    if temp == LeadTemperature.HOT:
        return Priority.P1
    if temp == LeadTemperature.WARM:
        # Upgrade to P1 if total historical value is very high
        if profile.total_order_value >= 10_000_000:
            return Priority.P1
        return Priority.P2
    # COLD
    return Priority.P3


def check_credit_risk(profile: CustomerProfile) -> tuple[bool, Optional[str]]:
    """
    Flag credit risk when outstanding > 80% of limit.
    This is a human-review trigger — same as in the spec.
    """
    if profile.customer_type == CustomerType.NEW:
        return False, None
    if profile.credit_limit > 0 and profile.credit_utilization_pct >= 80:
        return True, (f"Outstanding ₹{profile.outstanding_amount:,.0f} is "
                      f"{profile.credit_utilization_pct:.0f}% of credit limit "
                      f"₹{profile.credit_limit:,.0f}. Requires credit approval.")
    return False, None


# ---------------------------------------------------------------------------
# Output model
# ---------------------------------------------------------------------------

class QualificationResult(BaseModel):
    inquiry_id: str
    company_name: str
    customer_type: str

    score: int
    score_breakdown: dict        # serialised ScoreBreakdown
    temperature: LeadTemperature
    priority: Priority

    credit_risk_flag: bool = False
    credit_risk_reason: Optional[str] = None
    requires_human_review: bool = False
    human_review_reason: Optional[str] = None

    rationale: str               # LLM-generated explanation


# ---------------------------------------------------------------------------
# LLM rationale (explain the score in plain English for the sales team)
# ---------------------------------------------------------------------------

RATIONALE_PROMPT = """\
You are a B2B industrial sales analyst writing a brief qualification note.

Customer: {company} ({customer_type} customer)
Lead score: {score}/100  →  {temperature}  |  Priority: {priority}

Score signals:
{signals}

Write 3–4 sentences for the sales team:
- Why this lead scored {temperature}
- What to watch out for (if any)
- Recommended next action
Keep it direct, factual, no fluff.
"""


def generate_rationale(
    profile: CustomerProfile,
    breakdown: ScoreBreakdown,
    temp: LeadTemperature,
    priority: Priority,
    client: Optional[genai.Client],
    rag_context: Optional[AgentRAGContext] = None,
    customer_360: Optional[dict] = None,
    sales_context: Optional[dict] = None,
) -> str:
    signals = "\n".join(f"  • {r}" for r in breakdown.reasons)
    fallback = (
        f"{profile.company_name} is a {temp.value.upper()} lead "
        f"(score {breakdown.total}/100, priority {priority.value}). "
        f"Key signals: {'; '.join(breakdown.reasons[:3])}."
    )
    if client is None:
        return fallback

    prompt = RATIONALE_PROMPT.format(
        company=profile.company_name,
        customer_type=profile.customer_type.value,
        score=breakdown.total,
        temperature=temp.value.upper(),
        priority=priority.value,
        signals=signals,
    )
    if rag_context:
        prompt += (
            "\n\nRETRIEVED CUSTOMER RECORDS:\n"
            f"{rag_context.combined_text}\n"
            "Use these records only as supporting evidence. Do not override "
            "structured balances, credit limits, or payment data."
        )
    if customer_360:
        prompt += (
            "\n\nCUSTOMER 360 SUMMARY:\n"
            f"{json.dumps(customer_360.get('summary', {}), default=str)}\n"
            f"Preferences: {json.dumps(customer_360.get('preferences', {}), default=str)}\n"
            "Use this structured history to improve the recommendation."
        )
    if sales_context and sales_context.get("semantic_memories"):
        memories = "\n".join(
            f"- {item.get('content', '')}"
            for item in sales_context["semantic_memories"][:5]
        )
        prompt += (
            "\n\nRELEVANT RELATIONSHIP MEMORY:\n"
            f"{memories}\n"
            "Treat these as contextual notes, not authoritative balances or prices."
        )
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite", contents=prompt
        )
        return response.text.strip()
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Main entry: qualify_lead()
# ---------------------------------------------------------------------------

def qualify_lead(
    inquiry_id: str,
    profile: CustomerProfile,
    client: Optional[genai.Client] = None,
    rag_context: Optional[AgentRAGContext] = None,
    customer_360: Optional[dict] = None,
    sales_context: Optional[dict] = None,
) -> QualificationResult:

    breakdown   = compute_score(profile)
    temperature = classify_temperature(breakdown.total)
    priority    = assign_priority(temperature, profile)
    credit_risk, credit_reason = check_credit_risk(profile)
    rationale   = generate_rationale(
        profile,
        breakdown,
        temperature,
        priority,
        client,
        rag_context,
        customer_360,
        sales_context,
    )

    # Human review is needed for credit risk OR cold leads with large order signals
    human_review   = credit_risk
    human_reason   = credit_reason

    return QualificationResult(
        inquiry_id=inquiry_id,
        company_name=profile.company_name,
        customer_type=profile.customer_type.value,
        score=breakdown.total,
        score_breakdown={
            "order_history":  breakdown.order_history_score,
            "payment":        breakdown.payment_score,
            "credit_health":  breakdown.credit_health_score,
            "win_rate":       breakdown.win_rate_score,
            "recency":        breakdown.recency_score,
            "reasons":        breakdown.reasons,
        },
        temperature=temperature,
        priority=priority,
        credit_risk_flag=credit_risk,
        credit_risk_reason=credit_reason,
        requires_human_review=human_review,
        human_review_reason=human_reason,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Demo — runs 05 seed then qualifies both profiles
# ---------------------------------------------------------------------------

async def _demo():
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    lookup_mod = import_module("05_customer_qual")

    engine = create_async_engine("sqlite+aiosqlite:///sales_os.db")
    async with engine.begin() as conn:
        await conn.run_sync(ia.Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
             if os.environ.get("GEMINI_API_KEY") else None

    test_cases = [
        InquiryExtraction(
            inquiry_id="INQ-001", company_name="Apex Steel Pvt Ltd",
            customer_name="Ramesh Kumar", product_requested="MS Billet IS2062",
            quantity="500 MT", delivery_location="Ludhiana",
            extraction_confidence=0.95,
        ),
        InquiryExtraction(
            inquiry_id="INQ-002", company_name="Nova Auto Parts Ltd",
            customer_name="Priya Singh", product_requested="MS Sheet IS513",
            quantity="20 MT", delivery_location="Pune",
            extraction_confidence=0.90,
        ),
    ]

    async with Session() as session:
        for ext in test_cases:
            profile = await lookup_mod.lookup_customer(session, ext)
            result  = qualify_lead(ext.inquiry_id, profile, client)

            print(f"\n{'='*60}")
            print(f"Company    : {result.company_name}  ({result.customer_type})")
            print(f"Score      : {result.score}/100")
            print(f"Breakdown  : order={result.score_breakdown['order_history']}  "
                  f"payment={result.score_breakdown['payment']}  "
                  f"credit={result.score_breakdown['credit_health']}  "
                  f"win_rate={result.score_breakdown['win_rate']}  "
                  f"recency={result.score_breakdown['recency']}")
            print(f"Lead       : {result.temperature.value.upper()}  |  "
                  f"Priority: {result.priority.value}")
            print(f"Credit risk: {result.credit_risk_flag}"
                  + (f"  → {result.credit_risk_reason}" if result.credit_risk_flag else ""))
            print(f"Human review needed: {result.requires_human_review}")
            print(f"\nRationale:\n{result.rationale}")


if __name__ == "__main__":
    asyncio.run(_demo())
