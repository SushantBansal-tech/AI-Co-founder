from typing import Any

from pydantic import BaseModel, Field


class MemorySnippet(BaseModel):
    memory_id: str
    content: str
    content_type: str
    score: float
    occurred_at: str | None = None
    thread_id: str | None = None


class SalesContext(BaseModel):
    business_id: str
    customer_id: str
    agent_name: str
    query: str
    customer_360: dict[str, Any] = Field(default_factory=dict)
    semantic_memories: list[MemorySnippet] = Field(default_factory=list)
    semantic_memory_available: bool = True
    warnings: list[str] = Field(default_factory=list)

    @property
    def combined_text(self) -> str:
        customer = self.customer_360.get("customer", {})
        summary = self.customer_360.get("summary", {})
        preferences = self.customer_360.get("preferences", {})
        lines = [
            f"Customer: {customer.get('company_name') or customer.get('id', 'unknown')}",
            f"Type: {customer.get('customer_type', 'unknown')}",
            f"Outstanding amount: {customer.get('outstanding_amount', 0)}",
            f"Payment behavior: {customer.get('payment_behavior', 'unknown')}",
            f"Orders: {summary.get('total_orders', 0)}; quotation win rate: {summary.get('quotation_win_rate', 0)}%",
            f"Preferred products: {', '.join(preferences.get('products', [])) or 'unknown'}",
        ]
        if self.semantic_memories:
            lines.append("Relevant relationship memory:")
            lines.extend(f"- {item.content}" for item in self.semantic_memories)
        return "\n".join(lines)
