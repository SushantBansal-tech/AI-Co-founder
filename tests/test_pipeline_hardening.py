import importlib

from app.pipeline.contracts import (
    FailureCategory,
    PipelineStatus,
    classify_failure,
    failure_result,
)
from app.products.resolution import normalize_requirement, verify_structured_product


class Product:
    name = "MS Billet IS2062 E250"
    grade = "IS2062 E250"
    specifications = "15mm"


def test_product_code_and_grade_are_normalized_before_semantic_search():
    result = normalize_requirement(
        {"product_requested": "Steel billet MSB 001", "specifications": "IS2062 E250 15 mm"}
    )
    assert result["product_code"] == "MSB-001"
    assert result["grade"] == "E250"
    assert verify_structured_product(result, Product())["exact"] is True


def test_grade_substitution_is_rejected_deterministically():
    result = normalize_requirement(
        {"product_requested": "Steel billet MSB-001", "specifications": "IS2062 E350 15 mm"}
    )
    verification = verify_structured_product(result, Product())
    assert verification["exact"] is False
    assert any(item["field"] == "grade" for item in verification["mismatches"])


def test_failures_have_operational_categories_and_statuses():
    assert classify_failure("pricing", "No active GST row") == FailureCategory.MISSING_MASTER_DATA
    result = failure_result("dispatch_quotation", "SMTP provider timeout")
    assert result["failure"]["category"] == FailureCategory.RETRYABLE_ERROR.value
    assert result["pipeline_status"] == PipelineStatus.RETRY_SCHEDULED.value


def test_negotiation_decision_is_deterministic_and_credit_aware():
    pricing_mod = importlib.import_module("app.agents.10_pricing_agent")
    negotiation_mod = importlib.import_module("app.agents.16_negotion")
    pricing = pricing_mod.PricingResult(
        inquiry_id="INQ-1", product_code="MSB-001", product_name="Billet",
        quantity_mt=100, total_cost_per_mt=80_000,
        floor_price_per_mt=90_000, suggested_price_per_mt=100_000,
        final_price_per_mt_ex_gst=98_000, max_discount_pct=10,
        approval_limit_pct=5, min_margin_pct=10, gst_rate_pct=18,
        pricing_possible=True,
    )
    analysis = negotiation_mod.evaluate_counteroffer(
        96_000, pricing,
        decision_context={"credit_limit": 1_000_000, "credit_exposure_after_order": 9_600_000},
    )
    assert analysis.decision.value == "needs_approval"
    assert analysis.can_auto_approve is False
    assert analysis.resulting_margin_pct > 0
    assert "credit" in analysis.human_approval_reason.lower()
