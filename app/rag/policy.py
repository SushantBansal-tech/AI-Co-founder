# app/rag/policy.py

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentRetrievalPolicy:
    document_types: list[str]
    top_k: int = 5
    score_threshold: float = 0.55
    allowed_agents_filter: bool = True


AGENT_RETRIEVAL_POLICIES: dict[str, AgentRetrievalPolicy] = {
    "requirement_agent": AgentRetrievalPolicy(
        document_types=[
            "product_catalog",
            "technical_specification",
            "manufacturing_capability",
        ],
        top_k=5,
    ),

    "qualification_agent": AgentRetrievalPolicy(
        document_types=[
            "customer_history",
            "quotation_history",
            "order_history",
            "payment_history",
        ],
        top_k=6,
    ),

    "feasibility_agent": AgentRetrievalPolicy(
        document_types=[
            "inventory",
            "production_capacity",
            "delivery_policy",
            "manufacturing_capability",
        ],
        top_k=5,
    ),

    "pricing_agent": AgentRetrievalPolicy(
        document_types=[
            "pricing_sheet",
            "discount_policy",
            "tax_policy",
            "margin_policy",
            "freight_policy",
        ],
        top_k=6,
    ),

    "quotation_agent": AgentRetrievalPolicy(
        document_types=[
            "quotation_template",
            "payment_terms",
            "delivery_terms",
            "tax_policy",
        ],
        top_k=5,
    ),

    "negotiation_agent": AgentRetrievalPolicy(
        document_types=[
            "discount_policy",
            "approval_matrix",
            "margin_policy",
            "negotiation_policy",
        ],
        top_k=5,
    ),

    "purchase_order_agent": AgentRetrievalPolicy(
        document_types=[
            "purchase_order_policy",
            "payment_terms",
            "delivery_terms",
            "product_catalog",
        ],
        top_k=5,
    ),
}