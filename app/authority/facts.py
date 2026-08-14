from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ActionFacts(BaseModel):
    """Validated facts for a proposed action.

    Extra fields are retained for action-specific evolution, but authority
    evaluators only read explicitly named fields. Values supplied by an LLM
    must be replaced by authoritative repository values before evaluation.
    """

    model_config = ConfigDict(extra="allow")

    entity_type: str | None = None
    entity_id: str | None = None
    thread_id: str | None = None
    customer_id: str | None = None
    missing_information: list[str] = Field(default_factory=list)
    missing_master_data: list[str] = Field(default_factory=list)
    evidence_chunk_ids: list[str] = Field(default_factory=list)


class CommercialFacts(ActionFacts):
    quotation_value: Decimal = Field(default=Decimal("0"), ge=0)
    resulting_margin_pct: Decimal | None = Field(default=None, ge=-100, le=100)
    discount_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    proposed_price_per_unit: Decimal | None = Field(default=None, ge=0)
    floor_price_per_unit: Decimal | None = Field(default=None, ge=0)


class MessageFacts(ActionFacts):
    message_type: str = "routine"
    recipient: str | None = None
    channel: str | None = None
    customer_opted_out: bool = False
    provider_configured: bool = True
    contains_commercial_commitment: bool = False
    outbound_count_today: int = Field(default=0, ge=0)


class PurchaseOrderFacts(ActionFacts):
    mandatory_fields_complete: bool = True
    critical_mismatches: list[str] = Field(default_factory=list)
    minor_mismatches: list[str] = Field(default_factory=list)
    quotation_is_current: bool = True
    price_revalidated: bool = True
    inventory_revalidated: bool = True
    capacity_revalidated: bool = True
    delivery_revalidated: bool = True
    credit_revalidated: bool = True
    approvals_valid: bool = True


class SalesOrderFacts(PurchaseOrderFacts):
    po_validation_passed: bool = False
    inventory_reserved: bool = False
    production_allocated: bool = False


class CustomerMergeFacts(ActionFacts):
    source_customer_id: str | None = None
    target_customer_id: str | None = None
    conflicting_identities: list[str] = Field(default_factory=list)
    match_confidence: Decimal | None = Field(default=None, ge=0, le=1)


class CreditChangeFacts(ActionFacts):
    current_credit_limit: Decimal | None = Field(default=None, ge=0)
    requested_credit_limit: Decimal | None = Field(default=None, ge=0)
    current_credit_days: int | None = Field(default=None, ge=0)
    requested_credit_days: int | None = Field(default=None, ge=0)
    current_exposure: Decimal | None = Field(default=None, ge=0)
    overdue_amount: Decimal | None = Field(default=None, ge=0)


class DealCloseFacts(ActionFacts):
    outcome: str | None = None
    reason_code: str | None = None
    valid_contractual_acceptance: bool = False
    po_validated: bool = False
    revalidation_passed: bool = False


FACT_MODELS: dict[str, type[ActionFacts]] = {
    "quotation_create": CommercialFacts,
    "quotation_prepare": CommercialFacts,
    "prepare_quotation": CommercialFacts,
    "quotation_send": CommercialFacts,
    "discount_apply": CommercialFacts,
    "negotiation_response": CommercialFacts,
    "negotiation_send": CommercialFacts,
    "message_send": MessageFacts,
    "routine_reminder": MessageFacts,
    "po_validate": PurchaseOrderFacts,
    "po_accept": PurchaseOrderFacts,
    "order_accept": PurchaseOrderFacts,
    "sales_order_create": SalesOrderFacts,
    "customer_merge": CustomerMergeFacts,
    "credit_change": CreditChangeFacts,
    "credit_terms_change": CreditChangeFacts,
    "deal_close_won": DealCloseFacts,
    "deal_close_lost": DealCloseFacts,
}


def validate_action_facts(action_type: str, facts: dict[str, Any]) -> ActionFacts:
    model = FACT_MODELS.get(action_type, ActionFacts)
    return model.model_validate(facts)
