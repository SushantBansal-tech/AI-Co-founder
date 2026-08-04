from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class PipelineStatus(str, Enum):
    PROCESSING = "processing"
    AWAITING_CUSTOMER_INFORMATION = "awaiting_customer_information"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_CUSTOMER_REPLY = "awaiting_customer_reply"
    AWAITING_PURCHASE_ORDER = "awaiting_purchase_order"
    AWAITING_CORRECTED_PO = "awaiting_corrected_po"
    QUOTATION_DISPATCH_PENDING = "quotation_dispatch_pending"
    RETRY_SCHEDULED = "retry_scheduled"
    BLOCKED = "blocked"
    FAILED = "failed"
    CLOSED_LOST = "closed_lost"
    HANDED_OFF = "handed_off"


class BusinessMilestone(str, Enum):
    INQUIRY_CAPTURED = "inquiry_captured"
    REQUIREMENT_NORMALIZED = "requirement_normalized"
    PRODUCT_RESOLVED = "product_resolved"
    CUSTOMER_RESOLVED = "customer_resolved"
    QUALIFIED = "qualified"
    FEASIBILITY_CHECKED = "feasibility_checked"
    PRICED = "priced"
    QUOTATION_CREATED = "quotation_created"
    QUOTATION_SENT = "quotation_sent"
    FOLLOWUP_SENT = "followup_sent"
    COMMERCIALS_ACCEPTED = "commercials_accepted"
    PO_RECEIVED = "po_received"
    PO_VALIDATED = "po_validated"
    ORDER_WON = "order_won"
    SALES_ORDER_CREATED = "sales_order_created"
    HANDED_OFF = "handed_off"


class WaitingFor(str, Enum):
    CUSTOMER = "customer"
    SALES_MANAGER = "sales_manager"
    FINANCE_MANAGER = "finance_manager"
    PRODUCTION_MANAGER = "production_manager"
    CORRECTED_PO = "corrected_po"
    MASTER_DATA_ADMIN = "master_data_admin"
    EXTERNAL_SYSTEM = "external_system"
    NONE = "none"


class FailureCategory(str, Enum):
    RETRYABLE_ERROR = "retryable_error"
    VALIDATION_ERROR = "validation_error"
    MISSING_MASTER_DATA = "missing_master_data"
    POLICY_BLOCK = "policy_block"
    EXTERNAL_API_FAILURE = "external_api_failure"
    UNEXPECTED_SYSTEM_ERROR = "unexpected_system_error"


class PipelineFailure(BaseModel):
    category: FailureCategory
    code: str
    node: str
    message: str
    retryable: bool = False
    retry_count: int = 0
    next_retry_at: str | None = None
    details: dict = Field(default_factory=dict)
    occurred_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def classify_failure(node: str, message: str) -> FailureCategory:
    text = message.lower()
    if any(value in text for value in ("timeout", "temporarily", "rate limit", "connection reset")):
        return FailureCategory.RETRYABLE_ERROR
    if any(value in text for value in ("smtp", "whatsapp", "provider", "outbound dispatcher")):
        return FailureCategory.EXTERNAL_API_FAILURE
    if any(value in text for value in ("missing", "no active", "not found in postgresql", "catalog unavailable")):
        return FailureCategory.MISSING_MASTER_DATA
    if any(value in text for value in ("invalid", "required", "mismatch", "no draft")):
        return FailureCategory.VALIDATION_ERROR
    if any(value in text for value in ("policy", "credit limit", "below floor", "approval expired")):
        return FailureCategory.POLICY_BLOCK
    return FailureCategory.UNEXPECTED_SYSTEM_ERROR


def failure_result(node: str, error: Exception | str, *, code: str | None = None) -> dict:
    message = str(error)
    category = classify_failure(node, message)
    retryable = category in {
        FailureCategory.RETRYABLE_ERROR,
        FailureCategory.EXTERNAL_API_FAILURE,
    }
    if retryable:
        status = PipelineStatus.RETRY_SCHEDULED
        waiting_for = WaitingFor.EXTERNAL_SYSTEM
    elif category in {FailureCategory.MISSING_MASTER_DATA, FailureCategory.POLICY_BLOCK}:
        status = PipelineStatus.BLOCKED
        waiting_for = (
            WaitingFor.MASTER_DATA_ADMIN
            if category == FailureCategory.MISSING_MASTER_DATA
            else WaitingFor.SALES_MANAGER
        )
    else:
        status = PipelineStatus.FAILED
        waiting_for = WaitingFor.NONE
    failure = PipelineFailure(
        category=category,
        code=code or f"{node.upper()}_FAILED",
        node=node,
        message=message,
        retryable=retryable,
    )
    return {
        "error": f"{node}: {message}",
        "failure": failure.model_dump(mode="json"),
        "pipeline_status": status.value,
        "waiting_for": waiting_for.value,
        "status_reason": message,
        "current_node": node,
        "status_updated_at": failure.occurred_at,
        "stages_completed": [],
    }
