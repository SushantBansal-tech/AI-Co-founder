from fastapi import APIRouter, Depends, HTTPException, Request

from app.ai_actions.service import AIActionService
from app.authority.auth import AuthenticatedAIPrincipal, require_ai_principal
from app.crm.auth import AuthenticatedUser, require_permission


router = APIRouter(prefix="/crm/ai/actions", tags=["Jarvis action ledger"])
execution_router = APIRouter(prefix="/ai/actions", tags=["Jarvis action ledger"])


def _service(request: Request) -> AIActionService:
    service = getattr(request.app.state, "ai_action_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="AI action ledger is unavailable.")
    return service


@router.get("")
async def list_ai_actions(
    request: Request,
    status: str | None = None,
    limit: int = 100,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return {
        "items": await _service(request).list_for_user(
            user, status=status, limit=min(max(limit, 1), 500)
        )
    }


@router.get("/{action_id}")
async def get_ai_action(
    action_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return await _service(request).get_for_user(user, action_id)


@execution_router.get("/{action_id}")
async def get_own_ai_action(
    action_id: str,
    request: Request,
    principal: AuthenticatedAIPrincipal = Depends(require_ai_principal),
):
    row = await _service(request).get_for_principal(
        action_id=action_id, business_id=principal.business_id,
        principal_id=principal.principal_id,
    )
    return _service(request).payload(row)
