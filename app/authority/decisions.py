from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AuthorityOutcome(StrEnum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    REQUIRE_MORE_INFORMATION = "REQUIRE_MORE_INFORMATION"
    BLOCKED_MASTER_DATA = "BLOCKED_MASTER_DATA"


class AuthorityDecisionResult(BaseModel):
    decision: AuthorityOutcome
    action_type: str
    risk_level: str
    policy_code: str
    policy_id: str | None = None
    policy_version: int | None = None
    settings_version: int | None = None
    approval_role: str | None = None
    reasons: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    missing_master_data: list[str] = Field(default_factory=list)
    evaluated_facts: dict[str, Any] = Field(default_factory=dict)
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    decision_id: str | None = None
    approval_request_id: str | None = None

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)

