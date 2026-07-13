from typing import Any


def build_retrieval_query(
    agent_name: str,
    state: dict[str, Any],
) -> str:
    lead = state.get("lead", {})
    raw_message = state.get("raw_message", "")

    product = lead.get("product_requested", "")
    quantity = lead.get("quantity", "")
    specifications = lead.get("specifications", "")
    company = lead.get("company_name", "")
    customer_id = lead.get("customer_id", "")

    queries = {
        "requirement_understanding": f"""
            Find product catalog entries and technical specifications
            relevant to the customer's requested product.

            Product: {product}
            Quantity: {quantity}
            Specifications: {specifications}
            Customer message: {raw_message}
        """,

        "customer_qualification": f"""
            Find customer history, previous quotations, previous orders,
            payment behaviour and outstanding amounts.

            Customer ID: {customer_id}
            Company: {company}
            Product: {product}
        """,

        "internal_feasibility": f"""
            Find inventory, manufacturing capacity, technical capability
            and delivery restrictions for this requirement.

            Product: {product}
            Quantity: {quantity}
            Specifications: {specifications}
        """,

        "cost_and_pricing": f"""
            Find applicable product price, quantity slabs, tax rules,
            discount limits and required margin.

            Product: {product}
            Quantity: {quantity}
            Customer company: {company}
        """,

        "quotation_generation": f"""
            Find the quotation template, approved price, payment terms,
            taxes and delivery terms for this customer requirement.

            Product: {product}
            Quantity: {quantity}
            Company: {company}
        """,

        "negotiation_support": f"""
            Find applicable discount limits, margin protection rules,
            approval levels and customer payment risk.

            Product: {product}
            Company: {company}
            Customer request: {raw_message}
        """,
    }

    query = queries.get(agent_name)

    if not query:
        return raw_message

    return " ".join(query.split())