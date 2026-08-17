from typing import Any

from pydantic import BaseModel, Field


class ResumeAIActionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class AIActionListResponse(BaseModel):
    items: list[dict[str, Any]]
