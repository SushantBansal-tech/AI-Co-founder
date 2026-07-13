"""
Sub-problem: Pricing Document Parser

Parses all 6 pricing CSVs uploaded by business owner into typed lookup
structures that the pricing engine queries at runtime.

Documents handled:
  1. Price list         — base selling price per product
  2. Raw material cost  — RM + overhead per product
  3. Transport cost     — cost per MT per delivery zone
  4. Discount policy    — tiered by customer type + order value
  5. Margin rules       — min and target margin per product
  6. GST rates          — per product category

Design rule:
  - These are small structured docs → parse directly, no embeddings needed.
  - Re-parse on every upload event (fast, < 100 rows each).
  - All lookups are O(1) dict access at runtime.

Run:
    python 09_pricing_documents.py
"""

import io
import csv
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Sample CSVs — business owner uploads these via admin panel
# ---------------------------------------------------------------------------

SAMPLE_PRICE_LIST_CSV = """\
product_code,product_name,base_price_per_mt,currency,valid_until
MSB-001,MS Billet IS2062,14500,INR,2025-06-30
MSP-001,MS Plate IS2062,18000,INR,2025-06-30
MSA-001,MS Angle IS2062,16500,INR,2025-06-30
PIP-001,MS Pipe IS1239,22000,INR,2025-06-30
TMT-001,TMT Bar IS1786 Fe500,17500,INR,2025-06-30
IBE-001,I Beam IS2062,19000,INR,2025-06-30
CHN-001,Channel Section IS2062,18500,INR,2025-06-30
SHT-001,MS Sheet IS513,20000,INR,2025-06-30
"""

SAMPLE_RM_COST_CSV = """\
product_code,product_name,rm_cost_per_mt,manufacturing_overhead_pct
MSB-001,MS Billet IS2062,11500,8
MSP-001,MS Plate IS2062,14000,10
MSA-001,MS Angle IS2062,12800,9
PIP-001,MS Pipe IS1239,17000,11
TMT-001,TMT Bar IS1786 Fe500,13500,9
IBE-001,I Beam IS2062,14800,10
CHN-001,Channel Section IS2062,14200,10
SHT-001,MS Sheet IS513,15500,10
"""

# Cost per MT by delivery zone (from transport cost sheet)
SAMPLE_TRANSPORT_CSV = """\
zone,cost_per_mt
North,450
West,800
South,950
East,1100
Unknown,900
"""

# Discount policy — tiered by customer_type + order value
# approval_limit_pct = max discount sales can give WITHOUT human sign-off
# max_discount_pct   = absolute ceiling (human must approve above approval_limit)
SAMPLE_DISCOUNT_CSV = """\
customer_type,order_value_min,order_value_max,max_discount_pct,approval_limit_pct
new,0,499999,3,2
new,500000,1999999,5,3
new,2000000,999999999,7,4
existing,0,499999,5,3
existing,500000,1999999,8,5
existing,2000000,999999999,12,8
"""

SAMPLE_MARGIN_CSV = """\
product_code,product_name,min_margin_pct,target_margin_pct
MSB-001,MS Billet IS2062,8,15
MSP-001,MS Plate IS2062,10,18
MSA-001,MS Angle IS2062,9,16
PIP-001,MS Pipe IS1239,10,18
TMT-001,TMT Bar IS1786 Fe500,9,16
IBE-001,I Beam IS2062,10,17
CHN-001,Channel Section IS2062,10,17
SHT-001,MS Sheet IS513,10,17
"""

SAMPLE_GST_CSV = """\
product_category,gst_rate_pct
Steel Billet,18
Steel Plate,18
Structural Steel,18
Steel Pipe,18
Reinforcement Bar,18
Steel Sheet,18
"""


# ---------------------------------------------------------------------------
# Parsed structures
# ---------------------------------------------------------------------------

@dataclass
class PriceListEntry:
    product_code: str
    base_price_per_mt: float
    currency: str = "INR"
    valid_until: str = ""


@dataclass
class RMCostEntry:
    product_code: str
    rm_cost_per_mt: float
    manufacturing_overhead_pct: float

    @property
    def overhead_per_mt(self) -> float:
        return round(self.rm_cost_per_mt * self.manufacturing_overhead_pct / 100, 2)

    @property
    def total_production_cost_per_mt(self) -> float:
        return round(self.rm_cost_per_mt + self.overhead_per_mt, 2)


@dataclass
class DiscountBand:
    customer_type: str     # "new" or "existing"
    order_value_min: float
    order_value_max: float
    max_discount_pct: float       # ceiling — human must approve above approval_limit
    approval_limit_pct: float     # sales can apply up to this without approval


@dataclass
class MarginRule:
    product_code: str
    min_margin_pct: float     # floor — never quote below this
    target_margin_pct: float  # ideal selling price point


@dataclass
class PricingDocuments:
    """
    Single container passed to the pricing engine.
    Built once per session / on document upload event.
    """
    price_list:      dict[str, PriceListEntry]   = field(default_factory=dict)
    rm_costs:        dict[str, RMCostEntry]       = field(default_factory=dict)
    transport_costs: dict[str, float]             = field(default_factory=dict)  # zone → ₹/MT
    discount_bands:  list[DiscountBand]           = field(default_factory=list)  # sorted list
    margin_rules:    dict[str, MarginRule]        = field(default_factory=dict)
    gst_rates:       dict[str, float]             = field(default_factory=dict)  # category → %


# ---------------------------------------------------------------------------
# CSV parsers
# ---------------------------------------------------------------------------

def _parse_price_list(csv_text: str) -> dict[str, PriceListEntry]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return {
        r["product_code"]: PriceListEntry(
            product_code=r["product_code"],
            base_price_per_mt=float(r["base_price_per_mt"]),
            currency=r.get("currency", "INR"),
            valid_until=r.get("valid_until", ""),
        )
        for r in reader
    }


def _parse_rm_costs(csv_text: str) -> dict[str, RMCostEntry]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return {
        r["product_code"]: RMCostEntry(
            product_code=r["product_code"],
            rm_cost_per_mt=float(r["rm_cost_per_mt"]),
            manufacturing_overhead_pct=float(r["manufacturing_overhead_pct"]),
        )
        for r in reader
    }


def _parse_transport(csv_text: str) -> dict[str, float]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return {r["zone"]: float(r["cost_per_mt"]) for r in reader}


def _parse_discount_bands(csv_text: str) -> list[DiscountBand]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    bands = [
        DiscountBand(
            customer_type=r["customer_type"],
            order_value_min=float(r["order_value_min"]),
            order_value_max=float(r["order_value_max"]),
            max_discount_pct=float(r["max_discount_pct"]),
            approval_limit_pct=float(r["approval_limit_pct"]),
        )
        for r in reader
    ]
    # Sort so range lookups are predictable
    return sorted(bands, key=lambda b: (b.customer_type, b.order_value_min))


def _parse_margin_rules(csv_text: str) -> dict[str, MarginRule]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return {
        r["product_code"]: MarginRule(
            product_code=r["product_code"],
            min_margin_pct=float(r["min_margin_pct"]),
            target_margin_pct=float(r["target_margin_pct"]),
        )
        for r in reader
    }


def _parse_gst_rates(csv_text: str) -> dict[str, float]:
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    return {r["product_category"]: float(r["gst_rate_pct"]) for r in reader}


# ---------------------------------------------------------------------------
# Master loader — call once on startup / document upload
# ---------------------------------------------------------------------------

def load_pricing_documents(
    price_list_csv:   str = SAMPLE_PRICE_LIST_CSV,
    rm_cost_csv:      str = SAMPLE_RM_COST_CSV,
    transport_csv:    str = SAMPLE_TRANSPORT_CSV,
    discount_csv:     str = SAMPLE_DISCOUNT_CSV,
    margin_csv:       str = SAMPLE_MARGIN_CSV,
    gst_csv:          str = SAMPLE_GST_CSV,
) -> PricingDocuments:
    return PricingDocuments(
        price_list      = _parse_price_list(price_list_csv),
        rm_costs        = _parse_rm_costs(rm_cost_csv),
        transport_costs = _parse_transport(transport_csv),
        discount_bands  = _parse_discount_bands(discount_csv),
        margin_rules    = _parse_margin_rules(margin_csv),
        gst_rates       = _parse_gst_rates(gst_csv),
    )


# ---------------------------------------------------------------------------
# Runtime lookup helpers — used by pricing engine
# ---------------------------------------------------------------------------

def get_discount_band(
    docs: PricingDocuments,
    customer_type: str,     # "new" or "existing"
    order_value: float,
) -> Optional[DiscountBand]:
    """Return the matching discount band or None if no policy exists."""
    for band in docs.discount_bands:
        if (band.customer_type == customer_type and
                band.order_value_min <= order_value <= band.order_value_max):
            return band
    return None


def get_gst_rate(docs: PricingDocuments, product_category: str) -> float:
    """Returns GST rate; defaults to 18% if category not found."""
    return docs.gst_rates.get(product_category, 18.0)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    docs = load_pricing_documents()

    print(f"Price list entries   : {len(docs.price_list)}")
    print(f"RM cost entries      : {len(docs.rm_costs)}")
    print(f"Transport zones      : {len(docs.transport_costs)}")
    print(f"Discount bands       : {len(docs.discount_bands)}")
    print(f"Margin rules         : {len(docs.margin_rules)}")
    print(f"GST rate categories  : {len(docs.gst_rates)}")

    # Spot checks
    billet = docs.rm_costs["MSB-001"]
    print(f"\nMSB-001 RM cost    : ₹{billet.rm_cost_per_mt:,.0f}/MT")
    print(f"MSB-001 overhead   : ₹{billet.overhead_per_mt:,.0f}/MT ({billet.manufacturing_overhead_pct}%)")
    print(f"MSB-001 total cost : ₹{billet.total_production_cost_per_mt:,.0f}/MT")

    print(f"\nNorth zone transport : ₹{docs.transport_costs['North']:,.0f}/MT")

    band = get_discount_band(docs, "existing", 7_250_000)
    if band:
        print(f"\nDiscount band (existing, ₹72.5L): "
              f"max={band.max_discount_pct}%  approval_limit={band.approval_limit_pct}%")

    print(f"\nGST (Steel Billet) : {get_gst_rate(docs, 'Steel Billet')}%")