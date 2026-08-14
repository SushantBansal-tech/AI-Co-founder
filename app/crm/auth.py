import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Callable

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.crm import AuthSession, BusinessMembership, User


PASSWORD_ALGORITHM = "pbkdf2_sha256"
PASSWORD_ITERATIONS = int(os.getenv("CRM_PASSWORD_ITERATIONS", "310000"))
SESSION_HOURS = int(os.getenv("CRM_SESSION_HOURS", "12"))


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)

ROLES = {
    "admin",
    "sales_manager",
    "salesperson",
    "finance_manager",
    "production_manager",
    "viewer",
}

ROLE_PERMISSIONS = {
    "admin": {"*"},
    "sales_manager": {
        "crm:read_all", "customer:edit", "customer:assign",
        "lead:edit", "lead:assign", "lead:close", "lead:reopen",
        "task:read_all", "task:create", "task:edit_all",
        "activity:create", "approval:read", "approval:sales",
        "customer_merge:resolve",
    },
    "salesperson": {
        "crm:read_assigned", "customer:edit_assigned",
        "task:read_assigned", "task:create", "task:edit_assigned",
        "activity:create", "approval:read",
    },
    "finance_manager": {
        "crm:read_all", "task:read_assigned", "task:create",
        "task:edit_assigned", "activity:create", "approval:read",
        "approval:finance",
    },
    "production_manager": {
        "crm:read_all", "task:read_assigned", "task:create",
        "task:edit_assigned", "activity:create", "approval:read",
        "approval:production",
    },
    "viewer": {"crm:read_all", "approval:read"},
}


@dataclass(frozen=True)
class AuthenticatedUser:
    user_id: str
    business_id: str
    membership_id: str
    role: str
    email: str
    display_name: str

    def has_permission(self, permission: str) -> bool:
        permissions = ROLE_PERMISSIONS.get(self.role, set())
        return "*" in permissions or permission in permissions


def normalize_user_email(email: str) -> str:
    normalized = email.strip().lower()
    if normalized.count("@") != 1:
        raise ValueError("A valid email address is required.")
    local, domain = normalized.split("@", 1)
    if not local or not domain:
        raise ValueError("A valid email address is required.")
    return normalized


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PASSWORD_ITERATIONS
    )
    return "$".join((
        PASSWORD_ALGORITHM,
        str(PASSWORD_ITERATIONS),
        base64.urlsafe_b64encode(salt).decode("ascii"),
        base64.urlsafe_b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_text, salt_text, expected_text = encoded.split("$", 3)
        if algorithm != PASSWORD_ALGORITHM:
            return False
        iterations = int(iteration_text)
        salt = base64.urlsafe_b64decode(salt_text.encode("ascii"))
        expected = base64.urlsafe_b64decode(expected_text.encode("ascii"))
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def create_auth_session(
    session: AsyncSession,
    *,
    user_id: str,
    business_id: str,
) -> tuple[str, AuthSession]:
    token = secrets.token_urlsafe(48)
    now = utc_now()
    auth_session = AuthSession(
        user_id=user_id,
        business_id=business_id,
        token_hash=token_digest(token),
        expires_at=now + timedelta(hours=SESSION_HOURS),
        last_used_at=now,
    )
    session.add(auth_session)
    await session.flush()
    return token, auth_session


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


async def require_user(
    request: Request,
    authorization: str | None = Header(default=None),
) -> AuthenticatedUser:
    token = _bearer_token(authorization)
    session_factory = getattr(request.app.state, "session_factory", None)
    if session_factory is None:
        raise HTTPException(status_code=503, detail="Database session is unavailable.")

    now = utc_now()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(AuthSession, User, BusinessMembership)
                .join(User, User.id == AuthSession.user_id)
                .join(
                    BusinessMembership,
                    (BusinessMembership.user_id == User.id)
                    & (BusinessMembership.business_id == AuthSession.business_id),
                )
                .where(
                    AuthSession.token_hash == token_digest(token),
                    AuthSession.revoked_at.is_(None),
                    AuthSession.expires_at > now,
                    User.status == "active",
                    BusinessMembership.status == "active",
                )
            )
        ).one_or_none()
        if row is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Session is invalid or expired.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        auth_session, user, membership = row
        if membership.role not in ROLES:
            raise HTTPException(status_code=403, detail="Membership role is invalid.")
        auth_session.last_used_at = now
        await session.commit()
        return AuthenticatedUser(
            user_id=user.id,
            business_id=membership.business_id,
            membership_id=membership.id,
            role=membership.role,
            email=user.email,
            display_name=user.display_name,
        )


def require_permission(permission: str) -> Callable:
    async def dependency(
        user: AuthenticatedUser = Depends(require_user),
    ) -> AuthenticatedUser:
        if not user.has_permission(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission '{permission}' is required.",
            )
        return user

    return dependency
