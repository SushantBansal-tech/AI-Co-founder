"""
Sub-problem 6: Match extracted requirement to catalog — EXACT / NEAR / CUSTOM.
Sub-problem 7: Flag custom requirements and identify specification gaps.
Sub-problem 8: Produce a clean, internal-facing requirement summary.

Depends on:
  01_inquiry_extraction.py  → InquiryExtraction
  03_catalog_ingestion.py   → CatalogProduct, query_catalog

Output is a RequirementSummary — the single object passed downstream
to the Feasibility Agent and the Pricing Agent.

Threshold design:
  EXACT   similarity >= 0.82   → proceed directly to pricing
  NEAR    0.65 <= sim < 0.82   → proceed, but note gaps; may need clarification
  CUSTOM  similarity  < 0.65   → flag for human review before quoting

Run:
    GEMINI_API_KEY=xxx python 04_requirement_matching.py
"""

import os
import json
import sys
from enum import Enum
from typing import Optional
from importlib import import_module

import chromadb
from google import genai
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Imports from earlier modules
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.dirname(__file__))
catalog_mod = import_module("03_catalog")
inquiry_mod = import_module("01_Inquiry")

CatalogProduct   = catalog_mod.CatalogProduct
query_catalog    = catalog_mod.query_catalog
InquiryExtraction = inquiry_mod.InquiryExtraction
InquirySource    = inquiry_mod.InquirySource

# ---------------------------------------------------------------------------
# Tunable thresholds (move to config/env in prod)
# ---------------------------------------------------------------------------
EXACT_THRESHOLD  = 0.82
NEAR_THRESHOLD   = 0.65

# Keywords that suggest the customer is referencing an external drawing/spec sheet
TECHNICAL_DOC_KEYWORDS = ["drawing", "drg", "dwg", "datasheet", "spec sheet",
                           "as per attached", "refer attachment", "standard attached"]


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------

class MatchType(str, Enum):
    EXACT  = "exact"   # sim >= 0.82 — catalog product satisfies requirement
    NEAR   = "near"    # 0.65 <= sim < 0.82 — closest match, gaps exist
    CUSTOM = "custom"  # sim < 0.65 — nothing close in catalog


class GapAnalysis(BaseModel):
    gaps: list[str]           # specific things the customer needs that the match doesn't cover
    critical_gap: bool        # True → size/grade/standard mismatch, False → minor/clarifiable
    can_fulfill: bool         # agent's best guess; human overrides for CUSTOM
    notes: str = ""


class RequirementSummary(BaseModel):
    inquiry_id: str
    match_type: MatchType
    matched_product: Optional[CatalogProduct] = None
    similarity_score: float = 0.0
    gap_analysis: Optional[GapAnalysis] = None
    needs_technical_doc_review: bool = False   # customer mentioned drawings/attachments
    requires_human_review: bool = False        # triggers Human Approval Agent
    human_review_reason: Optional[str] = None
    summary_text: str                          # clean English summary for the sales team


# ---------------------------------------------------------------------------
# Step 1: Classify match type from similarity score
# ---------------------------------------------------------------------------

def classify_match(similarity: float) -> MatchType:
    if similarity >= EXACT_THRESHOLD:
        return MatchType.EXACT
    elif similarity >= NEAR_THRESHOLD:
        return MatchType.NEAR
    else:
        return MatchType.CUSTOM


# ---------------------------------------------------------------------------
# Step 2: Detect if customer is referring to external technical documents
# ---------------------------------------------------------------------------

def detect_technical_doc_reference(extraction: InquiryExtraction) -> bool:
    text = " ".join(filter(None, [
        extraction.product_requested,
        extraction.specifications,
    ])).lower()
    return any(kw in text for kw in TECHNICAL_DOC_KEYWORDS)


# ---------------------------------------------------------------------------
# Step 3: Gap analysis — what does the customer need that the match doesn't have?
# Only called for NEAR matches; CUSTOM gets a canned response; EXACT skips it.
# ---------------------------------------------------------------------------

GAP_ANALYSIS_PROMPT = """\
You are a technical sales analyst for an Indian industrial B2B company.

Customer requirement:
  Product     : {product_requested}
  Quantity    : {quantity}
  Specs       : {specifications}

Best matched catalog product:
  {catalog_text}

Task: Identify specification gaps between what the customer needs and what the catalog product offers.

Respond ONLY with a valid JSON object — no markdown, no extra text:
{{
  "gaps": ["<specific gap 1>", "<specific gap 2>"],
  "critical_gap": true_or_false,
  "can_fulfill": true_or_false,
  "notes": "<optional short note>"
}}

Rules:
- gaps: list each concrete mismatch (grade, size, standard, surface finish, etc.)
- critical_gap: true if grade or size is outside catalog range; false if minor/clarifiable
- can_fulfill: true if the standard product can likely serve the need; false if custom production needed
- If no gaps found, return empty gaps list and can_fulfill true
"""


def analyze_gaps(
    extraction: InquiryExtraction,
    matched: CatalogProduct,
    client: genai.Client,
) -> GapAnalysis:
    prompt = GAP_ANALYSIS_PROMPT.format(
        product_requested=extraction.product_requested or "not specified",
        quantity=extraction.quantity or "not specified",
        specifications=extraction.specifications or "none",
        catalog_text=matched.to_embed_text(),
    )
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-lite",
            contents=prompt,
            config={"response_mime_type": "application/json"},
        )
        data = json.loads(response.text)
        return GapAnalysis(**data)
    except Exception as e:
        # Fail safe: return unknown-gap state so human review is triggered
        return GapAnalysis(
            gaps=[f"Gap analysis failed: {e}"],
            critical_gap=True,
            can_fulfill=False,
            notes="Automated gap analysis unavailable — manual check required.",
        )


# ---------------------------------------------------------------------------
# Step 4: Decide whether human review is needed
# ---------------------------------------------------------------------------

def needs_human_review(
    match_type: MatchType,
    gap: Optional[GapAnalysis],
    needs_tech_doc: bool,
) -> tuple[bool, Optional[str]]:
    if match_type == MatchType.CUSTOM:
        return True, "No catalog match found — custom product or non-standard requirement."
    if match_type == MatchType.NEAR and gap and gap.critical_gap:
        return True, f"Near match but critical specification gap: {'; '.join(gap.gaps)}"
    if needs_tech_doc:
        return True, "Customer referenced external drawing or spec sheet — technical review needed."
    return False, None


# ---------------------------------------------------------------------------
# Step 5: Generate the plain-English summary for the sales team
# ---------------------------------------------------------------------------

SUMMARY_PROMPT = """\
Write a 3–4 sentence internal requirement summary for the sales team of an industrial B2B company.
Keep it direct and factual — no fluff.

Match type  : {match_type}
Customer needs: {product_requested}, Qty: {quantity}, Specs: {specifications}
Catalog match : {catalog_match}
Gaps found  : {gaps}
Recommended action: {action}

Write only the summary paragraph, no headings, no bullet points.
"""


def generate_summary_text(
    extraction: InquiryExtraction,
    match_type: MatchType,
    matched: Optional[CatalogProduct],
    gap: Optional[GapAnalysis],
    client: genai.Client,
) -> str:
    if match_type == MatchType.EXACT:
        action = "Proceed directly to feasibility check and pricing."
    elif match_type == MatchType.NEAR:
        action = "Proceed with nearest match; clarify gaps with customer or production team."
    else:
        action = "Escalate to product/production team before quoting — no standard match available."

    prompt = SUMMARY_PROMPT.format(
        match_type=match_type.value,
        product_requested=extraction.product_requested or "not specified",
        quantity=extraction.quantity or "not specified",
        specifications=extraction.specifications or "none",
        catalog_match=matched.to_embed_text() if matched else "None",
        gaps="; ".join(gap.gaps) if gap and gap.gaps else "None",
        action=action,
    )
    try:
        response = client.models.generate_content(model="gemini-3.1-flash-lite", contents=prompt)
        return response.text.strip()
    except Exception:
        # Deterministic fallback
        return (
            f"Customer requires {extraction.product_requested} (qty: {extraction.quantity}). "
            f"Match type: {match_type.value}. "
            f"{'Nearest catalog match: ' + matched.name if matched else 'No catalog match found.'} "
            f"{'Gaps: ' + '; '.join(gap.gaps) if gap and gap.gaps else ''}"
        )


# ---------------------------------------------------------------------------
# Main entry: match_requirement()
# Ties all 5 steps together into one RequirementSummary
# ---------------------------------------------------------------------------

def match_requirement(
    extraction: InquiryExtraction,
    collection: chromadb.Collection,
    client: genai.Client,
) -> RequirementSummary:
    # Build query text from what the customer asked for
    query_parts = [extraction.product_requested or ""]
    if extraction.specifications:
        query_parts.append(extraction.specifications)
    query_text = " ".join(filter(None, query_parts))

    # Step 1 — vector search
    matches = query_catalog(query_text, collection, client, n_results=3)
    top_product, top_score = matches[0] if matches else (None, 0.0)

    # Step 2 — classify
    match_type = classify_match(top_score)

    # Step 3 — gap analysis (only for NEAR; EXACT/CUSTOM skip)
    gap = None
    if match_type == MatchType.NEAR and top_product:
        gap = analyze_gaps(extraction, top_product, client)
    elif match_type == MatchType.CUSTOM:
        # No meaningful catalog match — synthesize a gap record for audit trail
        gap = GapAnalysis(
            gaps=["No standard catalog product matches this requirement."],
            critical_gap=True,
            can_fulfill=False,
            notes="Custom or non-standard product — requires production/engineering review.",
        )

    # Step 4 — technical doc flag + human review decision
    needs_tech = detect_technical_doc_reference(extraction)
    human_needed, review_reason = needs_human_review(match_type, gap, needs_tech)

    # Step 5 — summary text
    summary = generate_summary_text(extraction, match_type, top_product, gap, client)

    return RequirementSummary(
        inquiry_id=extraction.inquiry_id,
        match_type=match_type,
        matched_product=top_product,
        similarity_score=top_score,
        gap_analysis=gap,
        needs_technical_doc_review=needs_tech,
        requires_human_review=human_needed,
        human_review_reason=review_reason,
        summary_text=summary,
    )


# ---------------------------------------------------------------------------
# Demo — chains 01 → 03 → 04 end-to-end
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cat = import_module("03_catalog")
    parse_catalog_csv  = cat.parse_catalog_csv
    build_catalog_index = cat.build_catalog_index
    SAMPLE_CATALOG_CSV  = cat.SAMPLE_CATALOG_CSV

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    chroma  = chromadb.Client()

    products   = parse_catalog_csv(SAMPLE_CATALOG_CSV)
    collection = build_catalog_index(products, client, chroma)

    # Three test cases that hit all three match types
    test_cases = [
        InquiryExtraction(
            inquiry_id="INQ-001",
            product_requested="MS Billet IS2062 grade",
            quantity="500 MT",
            specifications="100x100mm square section",
            extraction_confidence=0.95,
        ),
        InquiryExtraction(
            inquiry_id="INQ-002",
            product_requested="MS Pipe 2 inch",
            quantity="200 MT",
            specifications="80x80mm square, non-standard section size",  # size not in catalog
            extraction_confidence=0.85,
        ),
        InquiryExtraction(
            inquiry_id="INQ-003",
            product_requested="High tensile alloy steel ASTM A36",
            quantity="100 MT",
            specifications="as per attached drawing DRG-2024-91",  # triggers tech doc flag
            extraction_confidence=0.7,
        ),
    ]

    for extraction in test_cases:
        print(f"\n{'='*65}")
        print(f"INQUIRY  : {extraction.inquiry_id}")
        print(f"PRODUCT  : {extraction.product_requested}")
        print(f"QTY      : {extraction.quantity}")
        print(f"SPECS    : {extraction.specifications}")

        result = match_requirement(extraction, collection, client)

        print(f"\nMATCH TYPE : {result.match_type.value.upper()}")
        print(f"SIMILARITY : {result.similarity_score}")
        if result.matched_product:
            print(f"MATCHED TO : {result.matched_product.product_code} — {result.matched_product.name}")
        if result.gap_analysis and result.gap_analysis.gaps:
            print(f"GAPS       : {result.gap_analysis.gaps}")
            print(f"CRITICAL   : {result.gap_analysis.critical_gap}")
        print(f"TECH DOC   : {result.needs_technical_doc_review}")
        print(f"HUMAN REVIEW NEEDED : {result.requires_human_review}")
        if result.human_review_reason:
            print(f"REASON     : {result.human_review_reason}")
        print(f"\nSUMMARY:\n{result.summary_text}")