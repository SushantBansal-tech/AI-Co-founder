"""
Sub-problem: Internal Feasibility Engine

Takes InventoryResult (from 07) and adds:
  1. Production capacity check — can we manufacture the shortage qty?
  2. Delivery timeline — how many days to customer location?
  3. Deadline check — does total lead time fit customer's requirement?
  4. Fulfillment decision — FROM_STOCK / FROM_PRODUCTION / PARTIAL_STOCK / CANNOT_FULFILL
  5. Risk flags — each risk has a type, severity, and description
  6. LLM narrative — plain English summary for sales team (Gemini, with fallback)

Human review triggers (matches spec exactly):
  - CANNOT_FULFILL
  - Deadline cannot be met (and customer gave a hard date)
  - Production required for a CUSTOM product (no standard spec)
  - Production qty > 2x weekly capacity (large capacity strain)

Depends on:
  inquiry_agent.py       → InquiryExtraction
  04_requirement_matching → RequirementSummary, MatchType
  06_customer_qualification → QualificationResult, Priority
  07_inventory_check.py  → InventoryResult, StockStatus

Run:
    GEMINI_API_KEY=xxx python 08_feasibility_engine.py
    (works without key too — fallback narrative is used)
"""

import io
import re
import csv
import os
import sys
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel
from google import genai
from app.rag.models import AgentRAGContext

sys.path.insert(0, os.path.dirname(__file__))
ia      = import_module("01_Inquiry")  # for Base, log_action, InquiryExtraction
req_mod = import_module("04_requirment")
qual_mod = import_module("06_customer")
inv_mod = import_module("07_inventory")

InquiryExtraction  = ia.InquiryExtraction
RequirementSummary = req_mod.RequirementSummary
MatchType          = req_mod.MatchType
QualificationResult = qual_mod.QualificationResult
Priority           = qual_mod.Priority
InventoryResult    = inv_mod.InventoryResult
StockStatus        = inv_mod.StockStatus
parse_quantity     = inv_mod.parse_quantity


# ---------------------------------------------------------------------------
# Sample CSVs (uploaded by business owner)
# ---------------------------------------------------------------------------

SAMPLE_CAPACITY_CSV = """\
product_code,product_name,weekly_capacity_mt,lead_time_days,min_order_qty_mt
MSB-001,MS Billet IS2062,500,14,50
MSP-001,MS Plate IS2062,150,21,10
MSA-001,MS Angle IS2062,200,14,10
PIP-001,MS Pipe IS1239,200,10,5
TMT-001,TMT Bar IS1786 Fe500,400,12,10
IBE-001,I Beam IS2062,100,18,5
CHN-001,Channel Section IS2062,120,16,5
SHT-001,MS Sheet IS513,250,10,5
"""

SAMPLE_DELIVERY_CSV = """\
city,zone,transit_days
Ludhiana,North,2
Jalandhar,North,2
Amritsar,North,3
Delhi,North,3
Jaipur,North,4
Mumbai,West,4
Pune,West,5
Ahmedabad,West,4
Surat,West,5
Chennai,South,5
Hyderabad,South,4
Bengaluru,South,5
Kolkata,East,5
Bhubaneswar,East,6
"""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class FulfillmentType(str, Enum):
    FROM_STOCK      = "from_stock"       # full qty available in warehouse
    FROM_PRODUCTION = "from_production"  # nothing in stock, full production run
    PARTIAL_STOCK   = "partial_stock"    # split: warehouse + production run
    CANNOT_FULFILL  = "cannot_fulfill"   # neither stock nor production capacity


class RiskSeverity(str, Enum):
    LOW    = "low"
    MEDIUM = "medium"
    HIGH   = "high"


class FeasibilityRisk(BaseModel):
    risk_type: str
    severity: RiskSeverity
    description: str


class FeasibilityResult(BaseModel):
    inquiry_id: str
    fulfillment_type: FulfillmentType

    # Quantities
    stock_qty: float = 0.0         # served from warehouse
    production_qty: float = 0.0    # needs to be manufactured

    # Timeline (days from today)
    production_lead_days: int = 0
    transit_days: int = 0
    total_lead_time_days: int = 0

    # Deadline
    customer_required_days: Optional[int] = None
    can_meet_deadline: bool = True

    # Delivery info
    delivery_location: str = ""
    delivery_zone: str = ""
    location_found: bool = False

    # Risks and human review
    risks: list[FeasibilityRisk] = []
    requires_human_review: bool = False
    human_review_reasons: list[str] = []

    narrative: str = ""   # LLM-generated plain English for sales team


# ---------------------------------------------------------------------------
# CSV parsers
# ---------------------------------------------------------------------------

def parse_capacity_csv(csv_text: str) -> dict[str, dict]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return {
        row["product_code"]: {
            "weekly_capacity_mt": float(row["weekly_capacity_mt"]),
            "lead_time_days":     int(row["lead_time_days"]),
            "min_order_qty_mt":   float(row["min_order_qty_mt"]),
        }
        for row in reader
    }


def parse_delivery_csv(csv_text: str) -> dict[str, dict]:
    """Returns dict keyed by lowercase city name."""
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return {
        row["city"].strip().lower(): {
            "zone": row["zone"],
            "transit_days": int(row["transit_days"]),
        }
        for row in reader
    }


# ---------------------------------------------------------------------------
# Delivery date parser — extract numeric days from customer string
# ---------------------------------------------------------------------------

def parse_required_days(delivery_date_str: Optional[str]) -> Optional[int]:
    """
    "ASAP" → 7,  "within 30 days" → 30,  "2 weeks" → 14, None → None
    """
    if not delivery_date_str:
        return None
    s = delivery_date_str.lower()
    if any(w in s for w in ("asap", "urgent", "immediately", "as soon")):
        return 7
    m = re.search(r"within\s+(\d+)\s*(day|week)", s)
    if m:
        n = int(m.group(1))
        return n if "day" in m.group(2) else n * 7
    m = re.search(r"(\d+)\s*(day|week)", s)
    if m:
        n = int(m.group(1))
        return n if "day" in m.group(2) else n * 7
    return None


# ---------------------------------------------------------------------------
# Delivery location resolver
# ---------------------------------------------------------------------------

def resolve_delivery(
    location_str: Optional[str],
    delivery_index: dict[str, dict],
    priority: Priority,
) -> tuple[str, int, bool]:
    """
    Returns (zone, transit_days, found).
    If location not found, uses a default based on priority:
      P1 → assume 3 days (be optimistic, flag risk)
      P2/P3 → assume 5 days (be conservative)
    """
    if not location_str:
        default = 3 if priority == Priority.P1 else 5
        return "Unknown", default, False

    # Try matching any word in the location string against our city index
    words = [w.strip().lower() for w in re.split(r"[,\-\s]+", location_str) if len(w) > 2]
    for word in words:
        if word in delivery_index:
            d = delivery_index[word]
            return d["zone"], d["transit_days"], True

    default = 3 if priority == Priority.P1 else 5
    return "Unknown", default, False


# ---------------------------------------------------------------------------
# Risk detection — pure deterministic rules
# ---------------------------------------------------------------------------

def detect_risks(
    inventory: InventoryResult,
    fulfillment: FulfillmentType,
    production_qty: float,
    capacity_info: Optional[dict],
    total_days: int,
    required_days: Optional[int],
    location_found: bool,
    match_type: MatchType,
) -> list[FeasibilityRisk]:
    risks: list[FeasibilityRisk] = []

    # 1. Cannot fulfill
    if fulfillment == FulfillmentType.CANNOT_FULFILL:
        risks.append(FeasibilityRisk(
            risk_type="no_fulfillment_path",
            severity=RiskSeverity.HIGH,
            description="Neither sufficient stock nor production capacity available for this order.",
        ))

    # 2. Deadline miss
    if required_days and total_days > required_days:
        overrun = total_days - required_days
        risks.append(FeasibilityRisk(
            risk_type="deadline_miss",
            severity=RiskSeverity.HIGH,
            description=(f"Customer needs delivery in {required_days} days but our "
                         f"total lead time is {total_days} days (+{overrun} days overrun)."),
        ))

    # 3. Production strain — order > 2x weekly capacity
    if capacity_info and production_qty > 2 * capacity_info["weekly_capacity_mt"]:
        risks.append(FeasibilityRisk(
            risk_type="high_production_load",
            severity=RiskSeverity.MEDIUM,
            description=(f"Production required ({production_qty:.0f} MT) exceeds "
                         f"2x weekly capacity ({capacity_info['weekly_capacity_mt']:.0f} MT/week). "
                         "Production team must confirm schedule."),
        ))

    # 4. Low stock cover on a partial order
    if inventory.stock_status == StockStatus.PARTIAL and inventory.stock_cover_pct < 30:
        risks.append(FeasibilityRisk(
            risk_type="low_stock_cover",
            severity=RiskSeverity.MEDIUM,
            description=(f"Stock covers only {inventory.stock_cover_pct:.0f}% of requested qty. "
                         "Almost entirely production-dependent."),
        ))

    # 5. Custom product in production
    if match_type == MatchType.CUSTOM and fulfillment != FulfillmentType.CANNOT_FULFILL:
        risks.append(FeasibilityRisk(
            risk_type="custom_product_production",
            severity=RiskSeverity.HIGH,
            description="Custom / non-standard product requires engineering review before production commitment.",
        ))

    # 6. Unknown delivery location
    if not location_found:
        risks.append(FeasibilityRisk(
            risk_type="unknown_delivery_location",
            severity=RiskSeverity.LOW,
            description="Delivery city not found in transport schedule. Transit days are estimated.",
        ))

    # 7. Tight timeline warning (within 2 days of deadline)
    if required_days and 0 < (required_days - total_days) <= 2:
        risks.append(FeasibilityRisk(
            risk_type="tight_deadline",
            severity=RiskSeverity.MEDIUM,
            description=(f"Only {required_days - total_days} day buffer before deadline. "
                         "Any production or dispatch delay will cause a miss."),
        ))

    return risks


# ---------------------------------------------------------------------------
# Human review decision
# ---------------------------------------------------------------------------

def needs_human_review(
    risks: list[FeasibilityRisk],
    fulfillment: FulfillmentType,
    match_type: MatchType,
) -> tuple[bool, list[str]]:
    reasons = []
    if fulfillment == FulfillmentType.CANNOT_FULFILL:
        reasons.append("Order cannot be fulfilled — human decision required.")
    for r in risks:
        if r.severity == RiskSeverity.HIGH:
            reasons.append(r.description)
    if match_type == MatchType.CUSTOM:
        reasons.append("Custom product — engineering sign-off needed before committing delivery.")
    return bool(reasons), list(dict.fromkeys(reasons))  # dedup, preserve order


# ---------------------------------------------------------------------------
# LLM narrative
# ---------------------------------------------------------------------------

NARRATIVE_PROMPT = """\
Write a 3-4 sentence feasibility summary for an industrial B2B sales executive.
Be direct and factual. No fluff.

Order: {product} | {qty} MT → {location}
Fulfillment: {fulfillment}
Stock available: {stock_qty} MT | Production needed: {prod_qty} MT
Total lead time: {total_days} days | Customer deadline: {deadline}
Key risks: {risks}

Include: what we can ship, how long it takes, any risk, recommended next action.
"""

def generate_narrative(
    result: FeasibilityResult,
    extraction: InquiryExtraction,
    client: Optional[genai.Client],
    rag_context: Optional[AgentRAGContext] = None,
) -> str:
    risks_text = "; ".join(r.description for r in result.risks) or "None"
    deadline = (f"{result.customer_required_days} days"
                if result.customer_required_days else "not specified")
    fallback = (
        f"Fulfillment type: {result.fulfillment_type.value}. "
        f"Stock: {result.stock_qty} MT, Production needed: {result.production_qty} MT. "
        f"Total lead time: {result.total_lead_time_days} days. "
        f"{'Deadline can be met.' if result.can_meet_deadline else 'Deadline CANNOT be met.'} "
        f"Risks: {risks_text}."
    )
    if client is None:
        return fallback
    try:
        prompt = NARRATIVE_PROMPT.format(
            product=extraction.product_requested or "N/A",
            qty=result.stock_qty + result.production_qty,
            location=result.delivery_location or "not specified",
            fulfillment=result.fulfillment_type.value,
            stock_qty=result.stock_qty,
            prod_qty=result.production_qty,
            total_days=result.total_lead_time_days,
            deadline=deadline,
            risks=risks_text,
        )
        if rag_context:
            prompt += (
                "\n\nRETRIEVED INTERNAL EVIDENCE:\n"
                f"{rag_context.combined_text}\n"
                "Use this only as supporting evidence. Structured inventory "
                "and lead-time calculations remain authoritative."
            )
        response = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
        return response.text.strip()
    except Exception:
        return fallback


# ---------------------------------------------------------------------------
# Main entry: check_feasibility()
# ---------------------------------------------------------------------------

def check_feasibility(
    extraction: InquiryExtraction,
    requirement: RequirementSummary,
    qualification: QualificationResult,
    inventory: InventoryResult,
    capacity_index: dict[str, dict],
    delivery_index: dict[str, dict],
    client: Optional[genai.Client] = None,
    rag_context: Optional[AgentRAGContext] = None,
) -> FeasibilityResult:

    req_qty, _ = parse_quantity(extraction.quantity)
    required_days = parse_required_days(extraction.delivery_date)
    priority = Priority(qualification.priority)

    # Resolve delivery location → transit days
    zone, transit_days, loc_found = resolve_delivery(
        extraction.delivery_location, delivery_index, priority
    )

    # Get production capacity for this product
    product_code = inventory.product_code or ""
    cap = capacity_index.get(product_code)

    # --- Determine fulfillment type ---
    stock_qty = 0.0
    production_qty = 0.0
    production_lead = 0

    if inventory.stock_status == StockStatus.NOT_IN_CATALOG:
        # No product in catalog at all
        fulfillment = FulfillmentType.CANNOT_FULFILL

    elif inventory.stock_status == StockStatus.SUFFICIENT:
        fulfillment = FulfillmentType.FROM_STOCK
        stock_qty = req_qty
        production_lead = 0

    elif inventory.stock_status == StockStatus.OUT_OF_STOCK:
        if cap:
            fulfillment = FulfillmentType.FROM_PRODUCTION
            production_qty = req_qty
            production_lead = cap["lead_time_days"]
        else:
            fulfillment = FulfillmentType.CANNOT_FULFILL

    else:  # PARTIAL
        stock_qty = inventory.available_qty
        production_qty = inventory.shortage_qty
        if cap:
            fulfillment = FulfillmentType.PARTIAL_STOCK
            production_lead = cap["lead_time_days"]
        else:
            # Have partial stock but can't produce the rest
            fulfillment = FulfillmentType.PARTIAL_STOCK
            production_lead = 0

    # Total lead time = production lead (if needed) + transit
    total_days = production_lead + transit_days
    can_meet = True if required_days is None else (total_days <= required_days)

    # Detect risks
    risks = detect_risks(
        inventory=inventory,
        fulfillment=fulfillment,
        production_qty=production_qty,
        capacity_info=cap,
        total_days=total_days,
        required_days=required_days,
        location_found=loc_found,
        match_type=requirement.match_type,
    )

    human_needed, human_reasons = needs_human_review(risks, fulfillment, requirement.match_type)

    result = FeasibilityResult(
        inquiry_id=extraction.inquiry_id,
        fulfillment_type=fulfillment,
        stock_qty=stock_qty,
        production_qty=production_qty,
        production_lead_days=production_lead,
        transit_days=transit_days,
        total_lead_time_days=total_days,
        customer_required_days=required_days,
        can_meet_deadline=can_meet,
        delivery_location=extraction.delivery_location or "",
        delivery_zone=zone,
        location_found=loc_found,
        risks=risks,
        requires_human_review=human_needed,
        human_review_reasons=human_reasons,
    )

    result.narrative = generate_narrative(
        result, extraction, client, rag_context
    )
    return result


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    cat_mod = import_module("03_catalog")
    inv_mod2 = import_module("07_inventory")

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"]) \
             if os.environ.get("GEMINI_API_KEY") else None

    capacity_index = parse_capacity_csv(SAMPLE_CAPACITY_CSV)
    delivery_index = parse_delivery_csv(SAMPLE_DELIVERY_CSV)
    inventory_index = inv_mod2.parse_inventory_csv(inv_mod2.SAMPLE_INVENTORY_CSV)

    test_cases = [
        # inquiry_id, product_code, product_name, qty, location, delivery_date, label
        ("INQ-001", "MSB-001", "MS Billet IS2062", "500 MT",  "Ludhiana",  "within 30 days", "EXACT, sufficient stock"),
        ("INQ-002", "MSB-001", "MS Billet IS2062", "1200 MT", "Pune",      "within 20 days", "PARTIAL stock, tight deadline"),
        ("INQ-003", "PIP-001", "MS Pipe IS1239",   "200 MT",  "Mumbai",    "within 45 days", "PARTIAL stock, comfortable deadline"),
        ("INQ-004", None,      "Custom ASTM A36",  "100 MT",  "Hyderabad", "within 15 days", "CUSTOM – cannot fulfill"),
    ]

    for inq_id, product_code, product_name, qty, location, del_date, label in test_cases:
        product = cat_mod.CatalogProduct(
            product_code=product_code or "CUSTOM",
            name=product_name, category="Steel", unit="MT"
        ) if product_code else None

        summary = RequirementSummary(
            inquiry_id=inq_id,
            match_type=MatchType.CUSTOM if not product_code else MatchType.EXACT,
            matched_product=product,
            similarity_score=0.9 if product_code else 0.3,
            summary_text="test",
        )
        extraction = InquiryExtraction(
            inquiry_id=inq_id,
            product_requested=product_name,
            quantity=qty,
            delivery_location=location,
            delivery_date=del_date,
            extraction_confidence=0.9,
        )
        qual = QualificationResult(
            inquiry_id=inq_id, company_name="Test Co",
            customer_type="existing", score=75,
            score_breakdown={}, temperature=qual_mod.LeadTemperature.HOT,
            priority=Priority.P1, rationale="test",
        )
        inv_result = inv_mod2.check_inventory(summary, extraction, inventory_index)
        result = check_feasibility(
            extraction, summary, qual,
            inv_result, capacity_index, delivery_index, client,
        )

        print(f"\n{'='*62}")
        print(f"[{label}]  {inq_id}")
        print(f"  Product   : {product_name}  |  Qty: {qty}  →  {location}")
        print(f"  Fulfillment: {result.fulfillment_type.value.upper()}")
        print(f"  Stock     : {result.stock_qty} MT  |  Production: {result.production_qty} MT")
        print(f"  Timeline  : {result.production_lead_days}d prod + {result.transit_days}d transit = {result.total_lead_time_days}d total")
        print(f"  Deadline  : required {result.customer_required_days}d  |  {'✓ MET' if result.can_meet_deadline else '✗ MISSED'}")
        if result.risks:
            print(f"  Risks     :")
            for r in result.risks:
                print(f"    [{r.severity.value.upper()}] {r.risk_type}: {r.description}")
        print(f"  Human review: {result.requires_human_review}")
        if result.human_review_reasons:
            for reason in result.human_review_reasons:
                print(f"    → {reason}")
        print(f"\n  Narrative:\n  {result.narrative}")
