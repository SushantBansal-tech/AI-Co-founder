from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class JarvisCommandRequest(BaseModel):
    conversation_id: str | None = Field(default=None, max_length=36)
    message: str = Field(min_length=1, max_length=10_000)


class JarvisConversationCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=255)


class JarvisActionResumeRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class ProposedToolCall(BaseModel):
    call_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    tool_name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=2000)


class JarvisPlan(BaseModel):
    interpretation: str = Field(min_length=1, max_length=4000)
    tool_calls: list[ProposedToolCall] = Field(default_factory=list, max_length=10)
    needs_clarification: bool = False
    clarification_question: str | None = Field(default=None, max_length=2000)

    @field_validator("tool_calls")
    @classmethod
    def unique_call_ids(cls, value: list[ProposedToolCall]):
        ids = [item.call_id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("Jarvis tool-call IDs must be unique within a run.")
        return value


class GroundedAnswer(BaseModel):
    answer: str = Field(min_length=1, max_length=20_000)


class JarvisCommandResponse(BaseModel):
    conversation_id: str
    run_id: str
    status: Literal["completed", "awaiting_approval", "blocked", "failed"]
    answer: str
    action_requests: list[dict[str, Any]] = Field(default_factory=list)
    supporting_data: list[dict[str, Any]] = Field(default_factory=list)
