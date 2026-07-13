"""
Sub-problem: PO Validator

Compares every extracted PO field against the final quotation version.
All comparisons are deterministic — no LLM.

Mismatch severity:
  CRITICAL → must be resolved before order can proceed
             (product, quantity, price, GST rate)
  WARNING  → proceed but flag for sales team
             (payment terms differ, delivery date tight)
  MINOR    → log only, no action needed
             (address format, special conditions)

Verdict:
  VALID            → all fields match — mark order as won
  MINOR_MISMATCH   → minor/warning only — mark won with notes
  CRITICAL_MISMATCH→ must ask customer or human for correction

Design: zero LLM calls. Every decision is arithmetic or string comparison.

Run:
    python 19_po_validator.py
"""

import re
import sys
import os
from enum import Enum
from typing import Optional
from importlib import import_module

from pydantic import BaseModel

sys.path.insert(0, os.path.dirname(__file__))
po_mod = import_module("18_PO")
qb     = import_module("11_quotation")
rv     = import_module("17_revised")

POExtraction      = po_mod.POExtraction
QuotationDraft    = qb.QuotationDraft
QuotationLineItem = qb.QuotationLineItem
VersionSummary    = rv.VersionSummary


# ── Severity and verdict enums ────────────────────────────────────────────

class MismatchSeverity(str, Enum):
    CRITICAL = "critical"   # block order progression
    WARNING  = "warning"    # note and proceed
    MINOR    = "minor"      # log only


class ValidationVerdict(str, Enum):
    VALID             = "valid"              # all matched
    MINOR_MISMATCH    = "minor_mismatch"     # only minor/warning gaps
    CRITICAL_MISMATCH = "critical_mismatch"  # at least one critical gap


# ── Mismatch record ───────────────────────────────────────────────────────

class FieldMismatch(BaseModel):
    field: str
    po_value: str
    quotation_value: str
    severity: MismatchSeverity
    description: str
    customer_action_needed: bool     # True → must ask customer to revise PO
    internal_action_needed: bool     # True → internal human needs to confirm


# ── Validation result ─────────────────────────────────────────────────────

class POValidationResult(BaseModel):
    verdict: ValidationVerdict
    mismatches: list[FieldMismatch]

    # Counts per severity
    critical_count: int = 0
    warning_count:  int = 0
    minor_count:    int = 0

    can_proceed: bool             # True if VALID or MINOR_MISMATCH
    requires_customer_correction: bool
    requires_human_review: bool

    # Messages for dispatch
    customer_correction_message: Optional[str] = None
    internal_note: Optional[str]               = None

    # Summary for audit
    summary: str


# ── Comparison helpers ────────────────────────────────────────────────────

_PRICE_TOLERANCE_PCT = 0.5     # ±0.5% rounding tolerance
_QTY_TOLERANCE_PCT   = 0.0     # exact match required for quantity


def _parse_num(val) -> Optional[float]:
    """Safely convert string or number to float."""
    if val is None:
        return None
    try:
        return float(str(val).replace(",", "").replace("₹", "").strip())
    except ValueError:
        return None


def _within_tolerance(a: float, b: float, pct: float) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / b * 100 <= pct


def _normalize_product(text: Optional[str]) -> set[str]:
    """Extract meaningful keywords for fuzzy product matching."""
    if not text:
        return set()
    # Keep alphanumeric tokens ≥ 2 chars, lowercase
    return {t.lower() for t in re.findall(r"[A-Za-z0-9]+", text) if len(t) >= 2}


def _normalize_terms(text: Optional[str]) -> str:
    """Strip punctuation + lowercase for payment terms comparison."""
    if not text:
        return ""
    return re.sub(r"[^\w\s]", "", text.lower()).strip()


# ── Individual field comparators ──────────────────────────────────────────

def _compare_product(
    po: POExtraction, draft: QuotationDraft
) -> Optional[FieldMismatch]:
    if not draft.line_items:
        return None
    q_desc = draft.line_items[0].description
    q_spec = draft.line_items[0].specification or ""

    po_keywords = _normalize_product(po.product_description)
    q_keywords  = _normalize_product(f"{q_desc} {q_spec}")

    overlap = po_keywords & q_keywords
    # If fewer than 2 significant keywords overlap → mismatch
    # Exclude very generic words
    generic = {"mt", "nos", "kg", "grade", "as", "per", "to"}
    sig_overlap = overlap - generic

    if len(sig_overlap) < 2:
        return FieldMismatch(
            field="product_description",
            po_value=po.product_description or "not specified",
            quotation_value=q_desc,
            severity=MismatchSeverity.CRITICAL,
            description=(
                f"Product mismatch: PO says '{po.product_description}' but "
                f"quotation is for '{q_desc}'. "
                f"Only {len(sig_overlap)} keyword(s) in common: {sig_overlap or 'none'}."
            ),
            customer_action_needed=True,
            internal_action_needed=True,
        )
    return None


def _compare_quantity(
    po: POExtraction, draft: QuotationDraft
) -> Optional[FieldMismatch]:
    if not draft.line_items or po.quantity is None:
        return None
    q_qty = draft.line_items[0].quantity
    po_qty = po.quantity

    if not _within_tolerance(po_qty, q_qty, _QTY_TOLERANCE_PCT):
        diff_pct = abs(po_qty - q_qty) / q_qty * 100
        sev = (
            MismatchSeverity.CRITICAL if diff_pct > 2
            else MismatchSeverity.WARNING
        )
        return FieldMismatch(
            field="quantity",
            po_value=f"{po_qty} {po.unit or 'MT'}",
            quotation_value=f"{q_qty} MT",
            severity=sev,
            description=(
                f"Quantity mismatch: PO has {po_qty} {po.unit or 'MT'}, "
                f"quotation has {q_qty} MT ({diff_pct:.1f}% difference)."
            ),
            customer_action_needed=sev == MismatchSeverity.CRITICAL,
            internal_action_needed=True,
        )
    return None


def _compare_price(
    po: POExtraction, draft: QuotationDraft
) -> Optional[FieldMismatch]:
    if not draft.line_items or po.price_per_unit_ex_gst is None:
        return None
    q_price = draft.line_items[0].discounted_price_ex_gst
    po_price = po.price_per_unit_ex_gst

    if not _within_tolerance(po_price, q_price, _PRICE_TOLERANCE_PCT):
        diff = po_price - q_price
        sev = MismatchSeverity.CRITICAL
        return FieldMismatch(
            field="price_per_unit_ex_gst",
            po_value=f"₹{po_price:,.2f}/MT",
            quotation_value=f"₹{q_price:,.2f}/MT",
            severity=sev,
            description=(
                f"Price mismatch: PO states ₹{po_price:,.0f}/MT, "
                f"quotation states ₹{q_price:,.0f}/MT "
                f"(difference: ₹{diff:+,.0f}/MT, {diff/q_price*100:+.2f}%)."
            ),
            customer_action_needed=True,
            internal_action_needed=True,
        )
    return None


def _compare_gst(
    po: POExtraction, draft: QuotationDraft
) -> Optional[FieldMismatch]:
    if not draft.line_items or po.gst_rate_pct is None:
        return None
    q_gst  = draft.line_items[0].gst_rate_pct
    po_gst = po.gst_rate_pct

    if abs(po_gst - q_gst) > 0.1:
        return FieldMismatch(
            field="gst_rate_pct",
            po_value=f"{po_gst:.0f}%",
            quotation_value=f"{q_gst:.0f}%",
            severity=MismatchSeverity.CRITICAL,
            description=(
                f"GST rate mismatch: PO has {po_gst:.0f}%, "
                f"quotation has {q_gst:.0f}%. "
                "This affects total invoice value and GST filing."
            ),
            customer_action_needed=True,
            internal_action_needed=True,
        )
    return None


def _compare_payment_terms(
    po: POExtraction, draft: QuotationDraft
) -> Optional[FieldMismatch]:
    q_terms = _normalize_terms(draft.payment_terms_text)
    po_terms = _normalize_terms(po.payment_terms)

    if not q_terms or not po_terms:
        return None

    # Extract % numbers from both strings
    q_nums = set(re.findall(r"\d+", q_terms))
    po_nums = set(re.findall(r"\d+", po_terms))

    # If both contain numbers and they differ significantly → WARNING
    if q_nums and po_nums and not q_nums.intersection(po_nums):
        return FieldMismatch(
            field="payment_terms",
            po_value=po.payment_terms or "",
            quotation_value=draft.payment_terms_text,
            severity=MismatchSeverity.WARNING,
            description=(
                f"Payment terms differ: PO says '{po.payment_terms}', "
                f"quotation says '{draft.payment_terms_text}'. "
                "Proceed but confirm with finance team."
            ),
            customer_action_needed=False,
            internal_action_needed=True,
        )
    return None


def _compare_total_amount(
    po: POExtraction, draft: QuotationDraft
) -> Optional[FieldMismatch]:
    if po.total_amount_inc_gst is None:
        return None
    q_total = draft.total_inc_gst
    po_total = po.total_amount_inc_gst

    if not _within_tolerance(po_total, q_total, 1.0):
        diff = po_total - q_total
        return FieldMismatch(
            field="total_amount_inc_gst",
            po_value=f"₹{po_total:,.0f}",
            quotation_value=f"₹{q_total:,.0f}",
            severity=MismatchSeverity.CRITICAL,
            description=(
                f"Total amount mismatch: PO states ₹{po_total:,.0f}, "
                f"quotation total is ₹{q_total:,.0f} "
                f"(difference: ₹{diff:+,.0f})."
            ),
            customer_action_needed=True,
            internal_action_needed=True,
        )
    return None


def _check_address_completeness(
    po: POExtraction,
) -> Optional[FieldMismatch]:
    if not po.shipping_address:
        return FieldMismatch(
            field="shipping_address",
            po_value="not provided",
            quotation_value="required for dispatch",
            severity=MismatchSeverity.WARNING,
            description=(
                "Shipping address missing in PO. "
                "Required for lorry receipt and dispatch documents."
            ),
            customer_action_needed=True,
            internal_action_needed=False,
        )
    return None


def _check_special_conditions(
    po: POExtraction,
) -> Optional[FieldMismatch]:
    if po.special_conditions:
        return FieldMismatch(
            field="special_conditions",
            po_value="; ".join(po.special_conditions),
            quotation_value="standard terms",
            severity=MismatchSeverity.MINOR,
            description=(
                f"PO has {len(po.special_conditions)} special condition(s) "
                "not covered in standard quotation terms. "
                "Review before dispatch."
            ),
            customer_action_needed=False,
            internal_action_needed=True,
        )
    return None


# ── Customer correction message ───────────────────────────────────────────

def _build_correction_message(
    po: POExtraction,
    draft: QuotationDraft,
    critical_mismatches: list[FieldMismatch],
) -> str:
    name = draft.buyer_contact or "Sir/Madam"
    q_no = draft.quotation_number
    po_no = po.po_number or "your PO"

    mismatch_lines = "\n".join(
        f"  • {m.field.replace('_', ' ').title()}: "
        f"PO has '{m.po_value}' but our quotation {q_no} states '{m.quotation_value}'"
        for m in critical_mismatches
    )

    return (
        f"Dear {name},\n\n"
        f"Thank you for sharing your Purchase Order {po_no}.\n\n"
        f"We have reviewed your PO against our Quotation {q_no} and found "
        f"the following discrepancies that need to be resolved before we can "
        f"proceed with the order:\n\n"
        f"{mismatch_lines}\n\n"
        f"Request you to please issue a revised PO with the corrected details, "
        f"or confirm in writing if you would like us to proceed on the terms "
        f"stated in our quotation.\n\n"
        f"We look forward to your prompt response.\n\n"
        f"Regards,\n{draft.seller_name}"
    )


# ── Main validator ────────────────────────────────────────────────────────

def validate_po(
    po: POExtraction,
    draft: QuotationDraft,
) -> POValidationResult:
    """
    Runs all field comparisons and returns a POValidationResult.
    Zero LLM calls — pure arithmetic and string matching.
    """
    # Run all comparisons
    comparators = [
        _compare_product,
        _compare_quantity,
        _compare_price,
        _compare_gst,
        _compare_payment_terms,
        _compare_total_amount,
    ]
    standalone = [
        _check_address_completeness,
        _check_special_conditions,
    ]

    mismatches: list[FieldMismatch] = []
    for fn in comparators:
        result = fn(po, draft)
        if result:
            mismatches.append(result)
    for fn in standalone:
        result = fn(po)
        if result:
            mismatches.append(result)

    # Counts
    critical = [m for m in mismatches if m.severity == MismatchSeverity.CRITICAL]
    warnings = [m for m in mismatches if m.severity == MismatchSeverity.WARNING]
    minor    = [m for m in mismatches if m.severity == MismatchSeverity.MINOR]

    # Verdict
    if critical:
        verdict = ValidationVerdict.CRITICAL_MISMATCH
    elif warnings or minor:
        verdict = ValidationVerdict.MINOR_MISMATCH
    else:
        verdict = ValidationVerdict.VALID

    can_proceed    = verdict != ValidationVerdict.CRITICAL_MISMATCH
    needs_customer = bool(critical) or any(m.customer_action_needed for m in warnings)
    needs_human    = any(m.internal_action_needed for m in mismatches)

    # Correction message
    correction_msg = None
    if needs_customer and critical:
        correction_msg = _build_correction_message(po, draft, critical)

    # Internal note
    internal_note = None
    if mismatches:
        lines = [f"PO Validation — {verdict.value.upper()}"]
        for m in mismatches:
            lines.append(f"  [{m.severity.value.upper()}] {m.field}: {m.description}")
        internal_note = "\n".join(lines)

    # Summary
    if verdict == ValidationVerdict.VALID:
        summary = "PO matches quotation on all checked fields. Ready to mark as won."
    elif verdict == ValidationVerdict.MINOR_MISMATCH:
        summary = (
            f"PO has {len(warnings)} warning(s) and {len(minor)} minor note(s). "
            "Can proceed — review flagged items before dispatch."
        )
    else:
        summary = (
            f"PO has {len(critical)} critical mismatch(es). "
            "Cannot proceed until resolved."
        )

    return POValidationResult(
        verdict=verdict,
        mismatches=mismatches,
        critical_count=len(critical),
        warning_count=len(warnings),
        minor_count=len(minor),
        can_proceed=can_proceed,
        requires_customer_correction=needs_customer and bool(critical),
        requires_human_review=needs_human,
        customer_correction_message=correction_msg,
        internal_note=internal_note,
        summary=summary,
    )


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Build a sample quotation draft
    sample_draft = qb.QuotationDraft(
        quotation_number="QT-2025-A1B2",
        inquiry_id="INQ-001",
        valid_until="30-07-2025",
        buyer_company="Apex Steel Pvt Ltd",
        buyer_contact="Ramesh Kumar",
        buyer_delivery_location="Ludhiana",
        seller_name="IndusSteel Trading Pvt. Ltd.",
        seller_email="sales@indussteel.in",
        subtotal_ex_gst=7_000_000,
        total_gst_amount=1_260_000,
        total_inc_gst=8_260_000,
        payment_terms_text="20% advance, balance net 45 days from dispatch",
        delivery_timeline="Ex-stock within 2-3 days.",
        line_items=[
            QuotationLineItem(
                sr_no=1, product_code="MSB-001",
                description="MS Billet IS2062",
                specification="100x100mm square section",
                quantity=500.0, unit="MT",
                unit_price_ex_gst=14500.0, discount_pct=3.5,
                discounted_price_ex_gst=14000.0,
                gst_rate_pct=18.0,
                gst_amount_per_unit=2520.0,
                total_inc_gst=8_260_000.0,
            )
        ],
    )

    test_cases = [
        (
            "Perfect PO — all fields match",
            POExtraction(
                po_number="APX-001", buyer_company="Apex Steel Pvt Ltd",
                product_description="MS Billet IS2062 100x100mm",
                quantity=500.0, unit="MT",
                price_per_unit_ex_gst=14000.0, gst_rate_pct=18.0,
                gst_amount=1_260_000, total_amount_inc_gst=8_260_000,
                payment_terms="20% advance balance 45 days",
                shipping_address="Sahnewal, Ludhiana",
                extraction_confidence=0.98,
            ),
        ),
        (
            "Price mismatch (PO says ₹13,500 vs quote ₹14,000)",
            POExtraction(
                po_number="APX-002", buyer_company="Apex Steel Pvt Ltd",
                product_description="MS Billet IS2062",
                quantity=500.0, unit="MT",
                price_per_unit_ex_gst=13500.0,
                gst_rate_pct=18.0, total_amount_inc_gst=7_965_000,
                extraction_confidence=0.95,
            ),
        ),
        (
            "Quantity + GST mismatch",
            POExtraction(
                po_number="APX-003", buyer_company="Apex Steel Pvt Ltd",
                product_description="MS Billet IS2062",
                quantity=450.0, unit="MT",
                price_per_unit_ex_gst=14000.0,
                gst_rate_pct=12.0,              # wrong GST
                total_amount_inc_gst=7_056_000,
                extraction_confidence=0.90,
            ),
        ),
        (
            "Different product + missing shipping address",
            POExtraction(
                po_number="APX-004", buyer_company="Apex Steel Pvt Ltd",
                product_description="HR Coil IS513 2mm",  # completely different
                quantity=500.0, unit="MT",
                price_per_unit_ex_gst=14000.0,
                gst_rate_pct=18.0, total_amount_inc_gst=8_260_000,
                special_conditions=["Material as per IS513", "No short supply"],
                extraction_confidence=0.88,
            ),
        ),
    ]

    for label, po_ext in test_cases:
        result = validate_po(po_ext, sample_draft)
        print(f"\n{'='*60}")
        print(f"[{label}]")
        print(f"  Verdict    : {result.verdict.value.upper()}")
        print(f"  Can proceed: {result.can_proceed}")
        print(f"  Mismatches : critical={result.critical_count}  "
              f"warning={result.warning_count}  minor={result.minor_count}")
        for m in result.mismatches:
            print(f"  [{m.severity.value.upper():8}] {m.field:30} "
                  f"PO={m.po_value!r:25} Q={m.quotation_value!r}")
        print(f"\n  Summary: {result.summary}")
        if result.customer_correction_message:
            print(f"\n  Customer message preview:\n"
                  f"  {result.customer_correction_message[:200]}...")