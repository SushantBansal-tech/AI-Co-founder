from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExecuteToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolExecutionResponse(BaseModel):
    execution_id: str
    tool_name: str
    status: Literal[
        "completed", "denied", "pending_approval",
        "needs_information", "blocked_master_data",
    ]
    authority_decision: str | None = None
    reason: str | None = None
    result: dict[str, Any] | None = None
    authority: dict[str, Any] | None = None


class SearchCustomersInput(BaseModel):
    search: str | None = Field(default=None, max_length=255)
    limit: int = Field(default=20, ge=1, le=50)


class SearchCustomersOutput(BaseModel):
    items: list[dict[str, Any]]
    count: int


class CustomerIdInput(BaseModel):
    customer_id: str = Field(min_length=1, max_length=100)


class Customer360Output(BaseModel):
    model_config = ConfigDict(extra="allow")
    customer: dict[str, Any]
    identities: list[dict[str, Any]]
    summary: dict[str, Any]
    preferences: dict[str, Any]


class LeadIdInput(BaseModel):
    lead_id: str = Field(min_length=1, max_length=100)


class LeadOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    business_id: str
    thread_id: str
    inquiry_id: str
    customer_id: str | None = None


class PipelineInput(BaseModel):
    thread_id: str | None = Field(default=None, max_length=100)
    lead_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def require_identifier(self):
        if not self.thread_id and not self.lead_id:
            raise ValueError("thread_id or lead_id is required")
        return self


class PipelineOutput(BaseModel):
    model_config = ConfigDict(extra="allow")
    thread_id: str
    pipeline_status: str
    waiting_for: str


class PendingApprovalsInput(BaseModel):
    limit: int = Field(default=50, ge=1, le=100)


class PendingApprovalsOutput(BaseModel):
    items: list[dict[str, Any]]
    count: int


class InventoryInput(BaseModel):
    product_code: str | None = Field(default=None, max_length=80)
    warehouse: str | None = Field(default=None, max_length=255)
    limit: int = Field(default=50, ge=1, le=100)


class InventoryOutput(BaseModel):
    items: list[dict[str, Any]]
    total_available_quantity: Decimal
    count: int


class PricingInputsInput(BaseModel):
    product_code: str = Field(min_length=1, max_length=80)
    destination_city: str | None = Field(default=None, max_length=120)
    customer_type: str | None = Field(default=None, max_length=80)
    order_value: Decimal | None = Field(default=None, ge=0)


class PricingInputsOutput(BaseModel):
    product_code: str
    prices: list[dict[str, Any]]
    costs: list[dict[str, Any]]
    transport: list[dict[str, Any]]
    discounts: list[dict[str, Any]]
    margins: list[dict[str, Any]]
    gst: list[dict[str, Any]]


class OpenTasksInput(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    assigned_to_user_id: str | None = None
    overdue_only: bool = False
    limit: int = Field(default=50, ge=1, le=100)


class OpenTasksOutput(BaseModel):
    items: list[dict[str, Any]]
    count: int


class AddCustomerNoteInput(BaseModel):
    customer_id: str
    content_type: Literal[
        "general", "preference", "negotiation", "payment", "relationship"
    ] = "general"
    content: str = Field(min_length=1, max_length=5000)
    thread_id: str | None = Field(default=None, max_length=100)


class CustomerNoteOutput(BaseModel):
    id: str
    customer_id: str
    content_type: str
    content: str
    status: str
    occurred_at: str


class CreateTaskInput(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    thread_id: str | None = Field(default=None, max_length=100)
    assigned_to_user_id: str
    task_type: Literal["follow_up", "call", "meeting", "review", "other"]
    title: str = Field(min_length=3, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    due_at: datetime


class TaskOutput(BaseModel):
    id: str
    customer_id: str | None = None
    lead_id: str | None = None
    assigned_to_user_id: str
    created_by_principal_id: str
    title: str
    status: str
    due_at: str


class RecordActivityInput(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    thread_id: str | None = Field(default=None, max_length=100)
    task_id: str | None = None
    activity_type: Literal["call", "email", "meeting", "internal_note", "follow_up"]
    subject: str = Field(min_length=3, max_length=255)
    notes: str | None = Field(default=None, max_length=5000)
    outcome: str | None = Field(default=None, max_length=100)
    occurred_at: datetime | None = None


class ActivityOutput(BaseModel):
    id: str
    activity_type: str
    subject: str
    outcome: str | None = None
    actor_principal_id: str
    occurred_at: str


class ScheduleFollowupInput(BaseModel):
    quotation_id: str
    max_attempts: int = Field(default=5, ge=1, le=10)


class ScheduleFollowupOutput(BaseModel):
    quotation_id: str
    quotation_number: str
    jobs_created: int
    status: str


class PrepareQuotationInput(BaseModel):
    lead_id: str
    product_code: str = Field(min_length=1, max_length=80)
    quantity: Decimal = Field(gt=0)
    requested_discount_pct: Decimal = Field(default=Decimal("0"), ge=0, le=100)
    validity_days: int = Field(default=15, ge=1, le=90)


class PreparedQuotationOutput(BaseModel):
    quotation_id: str
    quotation_number: str
    status: str
    product_code: str
    quantity: Decimal
    unit_price_ex_gst: Decimal
    discount_pct: Decimal
    resulting_margin_pct: Decimal
    subtotal_ex_gst: Decimal
    gst_rate_pct: Decimal
    gst_amount: Decimal
    total_inc_gst: Decimal
    valid_until: str
    requires_approval: bool
    dispatched: bool
