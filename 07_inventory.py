"""
Sub-problem: Inventory Check

Responsibilities:
  1. Parse inventory CSV uploaded by business owner
  2. Match product_code from RequirementSummary against inventory
  3. Extract numeric quantity from inquiry string ("500 MT", "200 nos")
  4. Return InventoryResult: available qty, warehouse, stock status, shortage

Depends on:
  inquiry_agent.py         → InquiryExtraction
  04_requirement_matching  → RequirementSummary

Design rule: ZERO LLM calls here. Every decision is deterministic math.
Quantity parsing uses regex, not Gemini.

Run:
    python 07_inventory_check.py
"""

import io
import re
import csv
import sys
import os
from enum import Enum
from typing import Optional
from importlib import import_module
from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(__file__))
ia  = import_module("01_Inquiry")  # for Base, log_action, InquiryExtraction
req = import_module("04_requirment")

InquiryExtraction    = ia.InquiryExtraction
RequirementSummary   = req.RequirementSummary


# ---------------------------------------------------------------------------
# Sample inventory CSV  (business owner uploads this via admin panel)
# In production: re-parse on every upload / nightly refresh
# ---------------------------------------------------------------------------

SAMPLE_INVENTORY_CSV = """\
product_code,product_name,available_qty,unit,warehouse_location,last_updated
MSB-001,MS Billet IS2062,850,MT,Warehouse A - Ludhiana,2025-01-15
MSP-001,MS Plate IS2062,45,MT,Warehouse C - Mumbai,2025-01-15
MSA-001,MS Angle IS2062,320,MT,Warehouse A - Ludhiana,2025-01-15
PIP-001,MS Pipe IS1239,120,MT,Warehouse B - Ludhiana,2025-01-15
TMT-001,TMT Bar IS1786 Fe500,300,MT,Warehouse A - Ludhiana,2025-01-15
IBE-001,I Beam IS2062,80,MT,Warehouse C - Mumbai,2025-01-15
CHN-001,Channel Section IS2062,60,MT,Warehouse A - Ludhiana,2025-01-15
SHT-001,MS Sheet IS513,190,MT,Warehouse B - Ludhiana,2025-01-15
"""


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class StockStatus(str, Enum):
    SUFFICIENT   = "sufficient"     # available >= requested
    PARTIAL      = "partial"        # 0 < available < requested
    OUT_OF_STOCK = "out_of_stock"   # available == 0
    NOT_IN_CATALOG = "not_in_catalog"  # product_code not found at all


class InventoryItem(BaseModel):
    product_code: str
    product_name: str
    available_qty: float
    unit: str
    warehouse_location: str
    last_updated: str


class InventoryResult(BaseModel):
    product_code: Optional[str]
    found_in_inventory: bool

    available_qty: float = 0.0
    unit: str = "MT"
    warehouse_location: str = ""
    last_updated: str = ""

    requested_qty: float = 0.0
    requested_unit: str = "MT"
    unit_mismatch: bool = False   # flag if customer's unit != inventory unit

    stock_status: StockStatus = StockStatus.NOT_IN_CATALOG
    shortage_qty: float = 0.0    # how much extra is needed beyond stock (0 = none)
    stock_cover_pct: float = 0.0 # what % of request stock can cover


# ---------------------------------------------------------------------------
# CSV parser
# ---------------------------------------------------------------------------

def parse_inventory_csv(csv_text: str) -> dict[str, InventoryItem]:
    """Returns dict keyed by product_code for O(1) lookup at runtime."""
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    index: dict[str, InventoryItem] = {}
    for row in reader:
        item = InventoryItem(
            product_code=row["product_code"].strip(),
            product_name=row["product_name"].strip(),
            available_qty=float(row["available_qty"]),
            unit=row["unit"].strip(),
            warehouse_location=row["warehouse_location"].strip(),
            last_updated=row["last_updated"].strip(),
        )
        index[item.product_code] = item
    return index


# ---------------------------------------------------------------------------
# Quantity string parser  ("500 MT" → (500.0, "MT"))
# Handles: "500 MT", "200 nos", "50", "1200 metric tons", "ASAP"
# ---------------------------------------------------------------------------

UNIT_ALIASES = {
    "metric ton": "MT", "metric tons": "MT", "tonnes": "MT", "tonne": "MT",
    "nos": "NOS", "no": "NOS", "numbers": "NOS", "units": "NOS", "pcs": "NOS",
    "kg": "KG", "kgs": "KG", "kilogram": "KG", "kilograms": "KG",
}

def parse_quantity(qty_str: Optional[str]) -> tuple[float, str]:
    """
    Returns (numeric_value, unit_string).
    Returns (0.0, 'MT') if unparseable — caller should flag as missing.
    """
    if not qty_str:
        return 0.0, "MT"

    text = qty_str.strip().lower()

    # Extract leading number (int or float)
    num_match = re.match(r"([\d,]+(?:\.\d+)?)", text)
    if not num_match:
        return 0.0, "MT"

    value = float(num_match.group(1).replace(",", ""))

    # Extract unit that follows the number
    rest = text[num_match.end():].strip()
    unit_raw = rest.split()[0] if rest else "MT"

    unit = UNIT_ALIASES.get(unit_raw, unit_raw.upper())
    return value, unit


# ---------------------------------------------------------------------------
# Core lookup
# ---------------------------------------------------------------------------

def check_inventory(
    requirement: RequirementSummary,
    extraction: InquiryExtraction,
    inventory_index: dict[str, InventoryItem],
) -> InventoryResult:
    """
    Looks up the matched product in inventory and computes stock status.
    If no catalog match (CUSTOM), returns NOT_IN_CATALOG immediately.
    """
    matched = requirement.matched_product
    if matched is None:
        return InventoryResult(
            product_code=None,
            found_in_inventory=False,
            stock_status=StockStatus.NOT_IN_CATALOG,
        )

    req_qty, req_unit = parse_quantity(extraction.quantity)

    item = inventory_index.get(matched.product_code)
    if item is None:
        return InventoryResult(
            product_code=matched.product_code,
            found_in_inventory=False,
            requested_qty=req_qty,
            requested_unit=req_unit,
            stock_status=StockStatus.NOT_IN_CATALOG,
        )

    # Unit mismatch check — flag but don't block (sales team resolves)
    unit_mismatch = (req_unit != item.unit) and req_unit not in ("", "MT")

    # Stock status
    if item.available_qty == 0:
        status = StockStatus.OUT_OF_STOCK
        shortage = req_qty
        cover_pct = 0.0
    elif item.available_qty >= req_qty:
        status = StockStatus.SUFFICIENT
        shortage = 0.0
        cover_pct = 100.0
    else:
        status = StockStatus.PARTIAL
        shortage = req_qty - item.available_qty
        cover_pct = round(item.available_qty / req_qty * 100, 1)

    return InventoryResult(
        product_code=matched.product_code,
        found_in_inventory=True,
        available_qty=item.available_qty,
        unit=item.unit,
        warehouse_location=item.warehouse_location,
        last_updated=item.last_updated,
        requested_qty=req_qty,
        requested_unit=req_unit,
        unit_mismatch=unit_mismatch,
        stock_status=status,
        shortage_qty=shortage,
        stock_cover_pct=cover_pct,
    )


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    inventory = parse_inventory_csv(SAMPLE_INVENTORY_CSV)
    print(f"Loaded {len(inventory)} inventory items\n")

    # Import catalog mod for RequirementSummary construction
    cat = import_module("03_catalog_ingestion")
    MatchType = req.MatchType

    test_cases = [
        # (product_code, product_name, qty_str, label)
        ("MSB-001", "MS Billet",          "500 MT",   "Stock sufficient"),
        ("MSB-001", "MS Billet",          "1200 MT",  "Partial stock – shortage"),
        ("PIP-001", "MS Pipe",            "200 MT",   "Partial stock – shortage"),
        ("SHT-001", "MS Sheet IS513",     "50 MT",    "Stock sufficient"),
        (None,      "Custom Alloy Steel", "100 MT",   "Custom – not in catalog"),
    ]

    for product_code, name, qty, label in test_cases:
        product = cat.CatalogProduct(
            product_code=product_code or "CUSTOM",
            name=name, category="Steel", unit="MT"
        ) if product_code else None

        summary = RequirementSummary(
            inquiry_id="TEST",
            match_type=MatchType.CUSTOM if not product_code else MatchType.EXACT,
            matched_product=product,
            similarity_score=0.9 if product_code else 0.3,
            summary_text="test",
        )
        extraction = InquiryExtraction(
            inquiry_id="TEST",
            product_requested=name,
            quantity=qty,
            extraction_confidence=0.9,
        )
        result = check_inventory(summary, extraction, inventory)

        print(f"[{label}]")
        print(f"  Product  : {product_code or 'NONE'}  |  Requested: {qty}")
        print(f"  Status   : {result.stock_status.value.upper()}")
        if result.found_in_inventory:
            print(f"  Available: {result.available_qty} {result.unit}  @  {result.warehouse_location}")
            print(f"  Cover    : {result.stock_cover_pct}%  |  Shortage: {result.shortage_qty} {result.unit}")
        print()