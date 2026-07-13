AGENT_DOCUMENT_POLICY: dict[str, list[str]] = {
    "requirement_understanding": [
        "product_catalog",
        "technical_specification",
        "manufacturing_capability",
    ],

    "customer_qualification": [
        "customer_history",
        "previous_orders",
        "previous_quotations",
        "payment_records",
    ],

    "internal_feasibility": [
        "product_catalog",
        "technical_specification",
        "manufacturing_capability",
        "inventory",
        "production_capacity",
        "delivery_policy",
    ],

    "cost_and_pricing": [
        "pricing_sheet",
        "discount_policy",
        "tax_policy",
        "margin_policy",
        "customer_history",
        "payment_records",
    ],

    "quotation_generation": [
        "quotation_template",
        "pricing_sheet",
        "tax_policy",
        "payment_terms",
        "delivery_terms",
    ],

    "follow_up_management": [
        "customer_history",
        "previous_quotations",
        "previous_orders",
    ],

    "negotiation_support": [
        "discount_policy",
        "margin_policy",
        "approval_matrix",
        "payment_records",
        "customer_history",
    ],

    "purchase_order_handling": [
        "purchase_order_policy",
        "payment_terms",
        "delivery_terms",
        "previous_quotations",
    ],

    "sales_order_handoff": [
        "sales_order_policy",
        "delivery_policy",
        "payment_terms",
    ],
}


def get_allowed_document_types(
    agent_name: str,
) -> list[str]:
    allowed_types = AGENT_DOCUMENT_POLICY.get(agent_name)

    if allowed_types is None:
        raise ValueError(
            f"No document policy configured for agent: {agent_name}"
        )

    return allowed_types