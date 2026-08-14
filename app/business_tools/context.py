from dataclasses import dataclass

from app.authority.auth import AuthenticatedAIPrincipal


@dataclass(frozen=True)
class ToolContext:
    business_id: str
    principal_id: str
    principal_name: str
    scopes: frozenset[str]
    execution_id: str
    idempotency_key: str | None

    @classmethod
    def from_principal(
        cls, principal: AuthenticatedAIPrincipal, *, execution_id: str,
        idempotency_key: str | None,
    ) -> "ToolContext":
        return cls(
            business_id=principal.business_id,
            principal_id=principal.principal_id,
            principal_name=principal.name,
            scopes=principal.scopes,
            execution_id=execution_id,
            idempotency_key=idempotency_key,
        )
