from typing import Any


AGENT_NAME_ALIASES = {
    "requirement_agent": "requirement_understanding",
    "qualification_agent": "customer_qualification",
    "feasibility_agent": "internal_feasibility",
    "pricing_agent": "cost_and_pricing",
    "quotation_agent": "quotation_generation",
    "followup_agent": "follow_up_management",
    "negotiation_agent": "negotiation_support",
    "purchase_order_agent": "purchase_order_handling",
    "handoff_agent": "sales_order_handoff",
}


def canonical_agent_name(agent_name: str) -> str:
    return AGENT_NAME_ALIASES.get(agent_name, agent_name)


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return vars(value) if hasattr(value, "__dict__") else {}


class AgentQueryBuilder:
    @staticmethod
    def build(agent_name: str, state: dict[str, Any]) -> str:
        agent_name = canonical_agent_name(agent_name)
        extraction = _as_dict(state.get("extraction") or state.get("inquiry_extraction"))
        requirement = _as_dict(state.get("requirement") or state.get("requirement_summary"))
        product = extraction.get("product_requested") or requirement.get("matched_product") or ""
        if isinstance(product, dict):
            product = product.get("name") or product.get("product_code") or str(product)
        quantity = extraction.get("quantity", "")
        specifications = extraction.get("specifications", "")
        company = extraction.get("company_name") or state.get("company_name", "")
        raw_text = state.get("raw_text") or state.get("raw_message") or ""

        queries = {
            "requirement_understanding": f"Product catalog and technical match for {product}. Specifications: {specifications}. Quantity: {quantity}. Request: {raw_text}",
            "customer_qualification": f"Customer history, quotations, orders and payment behaviour for {company}. Product: {product}",
            "internal_feasibility": f"Inventory, production capacity and delivery feasibility for {product}, quantity {quantity}. Requirement: {requirement}",
            "cost_and_pricing": f"Base price, discounts, tax, freight and minimum margin for {product}, quantity {quantity}, customer {company}",
            "quotation_generation": f"Quotation template, payment, tax and delivery terms for {product}. Pricing: {state.get('pricing') or state.get('pricing_result')}",
            "follow_up_management": f"Previous quotations, orders and customer follow-up history for {company}",
            "negotiation_support": f"Discount authority, minimum margin and negotiation policy for counteroffer {state.get('counteroffer') or state.get('customer_reply_text')}",
            "purchase_order_handling": f"Validate purchase order against quotation and approved terms. PO: {state.get('po_raw_text') or raw_text}",
            "sales_order_handoff": f"Sales order handoff, delivery and payment policy for {product}. PO validation: {state.get('po_validation')}",
        }
        query = queries.get(agent_name)
        if query is None:
            raise ValueError(f"No query builder configured for agent: {agent_name}")
        return " ".join(query.split())
