import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from fastapi import Header, HTTPException, Request
from sqlalchemy import select

from app.database.models.authority import AIPrincipalScope, AIServicePrincipal


def credential_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AuthenticatedAIPrincipal:
    principal_id: str
    business_id: str
    name: str
    scopes: frozenset[str]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


async def require_ai_principal(
    request: Request,
    x_ai_principal_token: str | None = Header(
        default=None, alias="X-AI-Principal-Token"
    ),
) -> AuthenticatedAIPrincipal:
    if not x_ai_principal_token:
        raise HTTPException(status_code=401, detail="AI principal token is required.")
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="Database session is unavailable.")
    now = datetime.now(UTC).replace(tzinfo=None)
    async with factory() as session:
        principal = await session.scalar(select(AIServicePrincipal).where(
            AIServicePrincipal.credential_hash == credential_digest(x_ai_principal_token),
            AIServicePrincipal.status == "active",
            AIServicePrincipal.revoked_at.is_(None),
        ))
        if principal is None:
            raise HTTPException(status_code=401, detail="AI principal token is invalid.")
        scopes = set((await session.scalars(select(AIPrincipalScope.scope).where(
            AIPrincipalScope.business_id == principal.business_id,
            AIPrincipalScope.principal_id == principal.id,
            AIPrincipalScope.revoked_at.is_(None),
        ))).all())
        principal.last_used_at = now
        await session.commit()
    return AuthenticatedAIPrincipal(
        principal_id=principal.id,
        business_id=principal.business_id,
        name=principal.name,
        scopes=frozenset(scopes),
    )
