# app/rag/query_builder.py

from typing import Any


class AgentQueryBuilder:
    @staticmethod
    def build(
        agent_name: str,
        state: dict[str, Any],
    ) -> str:
        if agent_name == "requirement_agent":
            return AgentQueryBuilder._requirement_query(state)

        if agent_name == "qualification_agent":
            return AgentQueryBuilder._qualification_query(state)

        if agent_name == "feasibility_agent":
            return AgentQueryBuilder._feasibility_query(state)

        if agent_name == "pricing_agent":
            return AgentQueryBuilder._pricing_query(state)

        if agent_name == "quotation_agent":
            return AgentQueryBuilder._quotation_query(state)

        if agent_name == "negotiation_agent":
            return AgentQueryBuilder._negotiation_query(state)

        raise ValueError(
            f"No query builder configured for agent: {agent_name}"
        )

    @staticmethod
    def _requirement_query(state: dict[str, Any]) -> str:
        inquiry = state["inquiry_extraction"]

        return (
            f"Product requested: {inquiry.product_requested}. "
            f"Specifications: {inquiry.specifications}. "
            f"Quantity: {inquiry.quantity}."
        )

    @staticmethod
    def _qualification_query(state: dict[str, Any]) -> str:
        customer = state.get("customer_name", "")
        company = state.get("company_name", "")

        return (
            f"Customer qualification history for customer {customer}, "
            f"company {company}. Include previous quotations, orders, "
            f"payments, outstanding balances and purchasing behavior."
        )

    @staticmethod
    def _feasibility_query(state: dict[str, Any]) -> str:
        requirement = state["requirement_summary"]

        return (
            f"Check inventory, production and delivery feasibility for "
            f"{requirement.matched_product}. "
            f"Customer quantity and specifications: "
            f"{requirement.summary_text}"
        )

    @staticmethod
    def _pricing_query(state: dict[str, Any]) -> str:
        requirement = state["requirement_summary"]
        qualification = state.get("qualification_summary")

        return (
            f"Calculate applicable price for "
            f"{requirement.matched_product}. "
            f"Required quantity: "
            f"{state['inquiry_extraction'].quantity}. "
            f"Customer qualification: {qualification}. "
            f"Find base price, quantity discount, customer discount, "
            f"tax, freight and minimum margin rules."
        )

    @staticmethod
    def _quotation_query(state: dict[str, Any]) -> str:
        return (
            f"Prepare quotation for pricing result "
            f"{state.get('pricing_result')}. "
            f"Retrieve quotation format, payment terms, delivery terms, "
            f"tax terms and validity conditions."
        )

    @staticmethod
    def _negotiation_query(state: dict[str, Any]) -> str:
        return (
            f"Evaluate customer counteroffer "
            f"{state.get('counteroffer')}. "
            f"Retrieve discount authority, minimum margin, "
            f"approval limits and negotiation policy."
        )