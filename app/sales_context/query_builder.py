def build_sales_memory_query(agent_name: str, state: dict) -> str:
    parts = [f"sales context for {agent_name}"]
    extraction = state.get("extraction") or {}
    requirement = state.get("requirement") or {}
    for value in (
        extraction.get("product_requested"), extraction.get("specifications"),
        extraction.get("quantity"), state.get("customer_reply_text"),
        state.get("po_raw_text"), requirement.get("summary_text"),
    ):
        if value:
            parts.append(str(value))
    if agent_name == "customer_qualification":
        parts.append("payment behavior credit risk negotiation history customer preferences")
    elif agent_name in {"followup", "follow_up_management"}:
        parts.append("preferred channel prior objections follow-up relationship")
    elif agent_name in {"negotiation", "negotiation_support", "pricing_agent", "cost_and_pricing"}:
        parts.append("previous discounts counter offers commercial preferences")
    elif agent_name == "purchase_order_handling":
        parts.append("purchase order exceptions payment and delivery preferences")
    return " ".join(parts)
