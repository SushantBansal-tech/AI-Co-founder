from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator


AIOperatingMode = Literal["recommend_only", "prepare_only", "execute_low_risk"]
DecisionMode = Literal[
    "deny", "recommend_only", "prepare_only", "approval_required",
    "auto_execute", "threshold_auto",
]
RiskLevel = Literal["low", "medium", "high", "critical"]


class BusinessSettingsUpdate(BaseModel):
    ai_operating_mode: AIOperatingMode
    currency: str = Field(default="INR", min_length=3, max_length=3)
    timezone: str = Field(default="Asia/Kolkata", min_length=1, max_length=80)
    maximum_automatic_discount_pct: Decimal = Field(ge=0, le=100)
    maximum_automatic_quotation_value: Decimal = Field(ge=0)
    minimum_margin_pct: Decimal = Field(ge=0, le=100)
    daily_outbound_message_limit: int = Field(ge=0, le=100000)
    default_approval_role: Literal[
        "admin", "sales_manager", "finance_manager", "production_manager"
    ] = "admin"
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=3, max_length=1000)

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class AIPrincipalCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    scopes: list[str] = Field(default_factory=list, max_length=30)


class ScopeChange(BaseModel):
    scope: str = Field(min_length=3, max_length=100)
    reason: str = Field(min_length=3, max_length=1000)


class ChangeReason(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class AuthorityPolicyUpdate(BaseModel):
    decision_mode: DecisionMode
    risk_level: RiskLevel
    required_scope: str = Field(min_length=3, max_length=100)
    approval_role: Literal[
        "admin", "sales_manager", "finance_manager", "production_manager"
    ] | None = None
    conditions: dict = Field(default_factory=dict)
    expected_version: int = Field(ge=1)
    change_reason: str = Field(min_length=3, max_length=1000)


class AuthorityEvaluationRequest(BaseModel):
    principal_id: str
    action_type: str = Field(min_length=3, max_length=80)
    facts: dict = Field(default_factory=dict)


class AIActionEvaluationRequest(BaseModel):
    action_type: str = Field(min_length=3, max_length=80)
    facts: dict = Field(default_factory=dict)


class AuthorityApprovalResolution(BaseModel):
    reason: str = Field(min_length=3, max_length=2000)
