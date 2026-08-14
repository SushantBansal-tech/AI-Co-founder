from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    business_id: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=12, max_length=200)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: dict


class UserCreateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=12, max_length=200)
    role: Literal[
        "admin", "sales_manager", "salesperson", "finance_manager",
        "production_manager", "viewer",
    ]


class CRMCustomerNoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    content_type: Literal[
        "call_summary", "meeting_note", "email_summary", "objection_summary",
        "relationship_note", "product_interest",
    ]
    thread_id: str | None = None
    interaction_id: str | None = None


class CRMApprovalRequest(BaseModel):
    approved_stage: Literal[
        "qualification", "requirement", "feasibility", "pricing",
        "negotiation", "po", "po_revalidation",
    ]


class CRMMatchDecisionRequest(BaseModel):
    action: Literal["merge", "keep_separate", "dismiss"]
    notes: str | None = Field(default=None, max_length=4000)


class CustomerUpdateRequest(BaseModel):
    company_name: str | None = Field(default=None, min_length=1, max_length=255)
    contact_person: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    city: str | None = Field(default=None, max_length=100)
    state: str | None = Field(default=None, max_length=100)
    gstin: str | None = Field(default=None, max_length=20)
    customer_type: str | None = Field(default=None, max_length=80)


class AssignmentRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=36)
    reason: str | None = Field(default=None, max_length=2000)


class LeadUpdateRequest(BaseModel):
    contact_person: str | None = Field(default=None, max_length=255)
    product_requested: str | None = Field(default=None, max_length=4000)
    quantity: str | None = Field(default=None, max_length=100)
    specifications: str | None = Field(default=None, max_length=4000)
    delivery_location: str | None = Field(default=None, max_length=255)
    delivery_date: str | None = Field(default=None, max_length=100)
    payment_expectation: str | None = Field(default=None, max_length=255)


class CloseLostRequest(BaseModel):
    reason_code: Literal[
        "price", "delivery_time", "payment_terms", "product_mismatch",
        "competitor", "no_response", "budget_cancelled",
        "duplicate_inquiry", "customer_postponed", "credit_rejected", "other",
    ]
    notes: str | None = Field(default=None, max_length=4000)
    competitor_name: str | None = Field(default=None, max_length=255)
    lost_value: Decimal | None = Field(default=None, ge=0)


class TaskCreateRequest(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    thread_id: str | None = None
    assigned_to_user_id: str
    task_type: Literal[
        "call", "email", "meeting", "follow_up", "document_collection",
        "quotation_review", "payment_follow_up", "internal_review",
    ]
    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10000)
    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    due_at: datetime


class TaskUpdateRequest(BaseModel):
    assigned_to_user_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10000)
    priority: Literal["low", "normal", "high", "urgent"] | None = None
    status: Literal["open", "in_progress"] | None = None
    due_at: datetime | None = None
    version: int = Field(ge=1)


class TaskCompletionRequest(BaseModel):
    completion_notes: str | None = Field(default=None, max_length=10000)
    version: int = Field(ge=1)


class ActivityCreateRequest(BaseModel):
    customer_id: str | None = None
    lead_id: str | None = None
    thread_id: str | None = None
    task_id: str | None = None
    activity_type: Literal[
        "call_completed", "meeting_completed", "customer_visited",
        "quotation_explained", "payment_discussed", "task_completed",
        "lead_reassigned",
    ]
    subject: str = Field(min_length=1, max_length=255)
    notes: str | None = Field(default=None, max_length=10000)
    outcome: str | None = Field(default=None, max_length=100)
    occurred_at: datetime | None = None
