from dataclasses import dataclass
from typing import Awaitable, Callable

from pydantic import BaseModel


ToolHandler = Callable[[object, BaseModel], Awaitable[dict]]


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    required_scope: str
    risk_level: str
    requires_idempotency: bool
    is_mutation: bool
    audit_event_type: str | None
    handler: ToolHandler
    authority_action: str | None = None
    permitted_authority_decisions: frozenset[str] = frozenset({"allow"})
    entity_type: str | None = None
    entity_id_field: str | None = None
