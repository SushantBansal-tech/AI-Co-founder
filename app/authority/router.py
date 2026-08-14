from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.authority.auth import AuthenticatedAIPrincipal, require_ai_principal
from app.authority.schemas import (
    AIActionEvaluationRequest,
    AIPrincipalCreate,
    AuthorityEvaluationRequest,
    AuthorityApprovalResolution,
    AuthorityPolicyUpdate,
    BusinessSettingsUpdate,
    ChangeReason,
    ScopeChange,
)
from app.authority.service import AuthorityService
from app.crm.auth import AuthenticatedUser, require_permission
from app.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    claim_request,
    complete_request,
    fail_request,
)


router = APIRouter(prefix="/crm/ai", tags=["AI authority"])
execution_router = APIRouter(prefix="/ai/authority", tags=["AI authority execution"])


def _service(request: Request) -> AuthorityService:
    service = getattr(request.app.state, "authority_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Authority service is unavailable.")
    return service


async def _claim(request: Request, user, endpoint: str, key: str, payload: dict):
    try:
        return await claim_request(
            business_id=user.business_id,
            endpoint=endpoint,
            idempotency_key=key,
            payload=payload,
            session_factory=_service(request).session_factory,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _mutate(request: Request, claim, operation):
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await operation()
        await complete_request(
            claim.event_id, response,
            session_factory=_service(request).session_factory,
        )
        return response
    except Exception as exc:
        await fail_request(
            claim.event_id, exc,
            session_factory=_service(request).session_factory,
        )
        raise


@router.get("/settings")
async def get_business_ai_settings(
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return await _service(request).get_settings(user)


@router.put("/settings")
async def update_business_ai_settings(
    body: BusinessSettingsUpdate,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("authority:modify")),
):
    payload = body.model_dump(mode="json")
    claim = await _claim(request, user, "/crm/ai/settings", idempotency_key, payload)
    return await _mutate(
        request, claim, lambda: _service(request).update_settings(user, body.model_dump())
    )


@router.get("/settings/history")
async def business_ai_settings_history(
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return {"items": await _service(request).settings_history(user)}


@router.get("/principals")
async def list_ai_principals(
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("principal:read")),
):
    return {"items": await _service(request).list_principals(user)}


@router.post("/principals")
async def create_ai_principal(
    body: AIPrincipalCreate,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("principal:modify")),
):
    payload = body.model_dump()
    claim = await _claim(request, user, "/crm/ai/principals", idempotency_key, payload)
    return await _mutate(
        request, claim, lambda: _service(request).create_principal(user, **payload)
    )


@router.post("/principals/{principal_id}/rotate")
async def rotate_ai_principal(
    principal_id: str,
    body: ChangeReason,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("principal:modify")),
):
    payload = {"principal_id": principal_id, **body.model_dump()}
    claim = await _claim(request, user, f"/crm/ai/principals/{principal_id}/rotate", idempotency_key, payload)
    return await _mutate(
        request, claim, lambda: _service(request).rotate_principal(user, principal_id, body.reason)
    )


@router.post("/principals/{principal_id}/revoke")
async def revoke_ai_principal(
    principal_id: str,
    body: ChangeReason,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("principal:modify")),
):
    payload = {"principal_id": principal_id, **body.model_dump()}
    claim = await _claim(request, user, f"/crm/ai/principals/{principal_id}/revoke", idempotency_key, payload)
    return await _mutate(
        request, claim, lambda: _service(request).revoke_principal(user, principal_id, body.reason)
    )


@router.post("/principals/{principal_id}/scopes")
async def grant_ai_scope(
    principal_id: str,
    body: ScopeChange,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("principal:modify")),
):
    payload = {"principal_id": principal_id, **body.model_dump(), "grant": True}
    claim = await _claim(request, user, f"/crm/ai/principals/{principal_id}/scopes", idempotency_key, payload)
    return await _mutate(
        request, claim,
        lambda: _service(request).change_scope(
            user, principal_id, body.scope, body.reason, grant=True
        ),
    )


@router.delete("/principals/{principal_id}/scopes/{scope}")
async def revoke_ai_scope(
    principal_id: str,
    scope: str,
    request: Request,
    reason: str,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("principal:modify")),
):
    payload = {"principal_id": principal_id, "scope": scope, "reason": reason, "grant": False}
    claim = await _claim(request, user, f"/crm/ai/principals/{principal_id}/scopes/{scope}", idempotency_key, payload)
    return await _mutate(
        request, claim,
        lambda: _service(request).change_scope(
            user, principal_id, scope, reason, grant=False
        ),
    )


@router.get("/policies")
async def list_authority_policies(
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return {"items": await _service(request).list_policies(user)}


@router.put("/policies/{policy_id}")
async def update_authority_policy(
    policy_id: str,
    body: AuthorityPolicyUpdate,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("authority:modify")),
):
    payload = {"policy_id": policy_id, **body.model_dump(mode="json")}
    claim = await _claim(request, user, f"/crm/ai/policies/{policy_id}", idempotency_key, payload)
    return await _mutate(
        request, claim, lambda: _service(request).update_policy(user, policy_id, body.model_dump())
    )


@router.get("/policies/{policy_id}/history")
async def authority_policy_history(
    policy_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return {"items": await _service(request).policy_history(user, policy_id)}


@router.post("/evaluate")
async def founder_preview_authority_decision(
    body: AuthorityEvaluationRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return await _service(request).evaluate(
        business_id=user.business_id,
        principal_id=body.principal_id,
        action_type=body.action_type,
        facts=body.facts,
    )


@router.post("/evaluate-deterministic")
async def founder_preview_deterministic_decision(
    body: AuthorityEvaluationRequest,
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    result = await _service(request).evaluate_action(
        business_id=user.business_id,
        principal_id=body.principal_id,
        action_type=body.action_type,
        facts=body.facts,
        create_approval=False,
    )
    return result.model_dump(mode="json")


@router.get("/decisions")
async def list_authority_decisions(
    request: Request,
    limit: int = 100,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return {"items": await _service(request).list_decisions(user, limit=min(limit, 500))}


@router.get("/decisions/{decision_id}")
async def get_authority_decision(
    decision_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return await _service(request).get_decision(user, decision_id)


@router.get("/approvals")
async def list_authority_approvals(
    request: Request,
    status: str | None = "PENDING",
    limit: int = 100,
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    return {
        "items": await _service(request).list_approval_requests(
            user, status=status, limit=min(limit, 500)
        )
    }


@router.post("/approvals/{approval_id}/approve")
async def approve_authority_action(
    approval_id: str,
    body: AuthorityApprovalResolution,
    request: Request,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    payload = {"approval_id": approval_id, "approve": True, **body.model_dump()}
    claim = await _claim(
        request, user, f"/crm/ai/approvals/{approval_id}/approve",
        idempotency_key, payload,
    )
    return await _mutate(
        request, claim,
        lambda: _service(request).resolve_approval(
            user, approval_id, approve=True, reason=body.reason
        ),
    )


@router.post("/approvals/{approval_id}/reject")
async def reject_authority_action(
    approval_id: str,
    body: AuthorityApprovalResolution,
    request: Request,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
    user: AuthenticatedUser = Depends(require_permission("authority:read")),
):
    payload = {"approval_id": approval_id, "approve": False, **body.model_dump()}
    claim = await _claim(
        request, user, f"/crm/ai/approvals/{approval_id}/reject",
        idempotency_key, payload,
    )
    return await _mutate(
        request, claim,
        lambda: _service(request).resolve_approval(
            user, approval_id, approve=False, reason=body.reason
        ),
    )


@execution_router.post("/evaluate")
async def evaluate_for_ai_principal(
    body: AIActionEvaluationRequest,
    request: Request,
    principal: AuthenticatedAIPrincipal = Depends(require_ai_principal),
):
    return await _service(request).evaluate(
        business_id=principal.business_id,
        principal_id=principal.principal_id,
        action_type=body.action_type,
        facts=body.facts,
    )


@execution_router.post("/evaluate-deterministic")
async def evaluate_deterministically_for_ai_principal(
    body: AIActionEvaluationRequest,
    request: Request,
    principal: AuthenticatedAIPrincipal = Depends(require_ai_principal),
):
    result = await _service(request).evaluate_action(
        business_id=principal.business_id,
        principal_id=principal.principal_id,
        action_type=body.action_type,
        facts=body.facts,
    )
    return result.model_dump(mode="json")
