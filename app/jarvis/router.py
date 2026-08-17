from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.crm.auth import AuthenticatedUser, require_permission
from app.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    claim_request,
    complete_request,
    fail_request,
)
from app.jarvis.schemas import (
    JarvisActionResumeRequest,
    JarvisCommandRequest,
    JarvisConversationCreateRequest,
)
from app.jarvis.service import JarvisService


router = APIRouter(prefix="/crm/jarvis", tags=["Jarvis CRM orchestrator"])


def _service(request: Request) -> JarvisService:
    service = getattr(request.app.state, "jarvis_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Jarvis service is unavailable.")
    return service


def _factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="Database session is unavailable.")
    return factory


async def _claim(request, user, endpoint, key, payload):
    try:
        return await claim_request(
            business_id=user.business_id,
            endpoint=endpoint,
            idempotency_key=key,
            payload=payload,
            session_factory=_factory(request),
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/conversations")
async def create_jarvis_conversation(
    body: JarvisConversationCreateRequest,
    request: Request,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
    user: AuthenticatedUser = Depends(require_permission("*")),
):
    claim = await _claim(
        request, user, "/crm/jarvis/conversations",
        idempotency_key, body.model_dump(mode="json"),
    )
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).create_conversation(user, body.title)
        await complete_request(
            claim.event_id, response, session_factory=_factory(request)
        )
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc, session_factory=_factory(request))
        raise


@router.get("/conversations")
async def list_jarvis_conversations(
    request: Request,
    limit: int = 50,
    user: AuthenticatedUser = Depends(require_permission("*")),
):
    return {
        "items": await _service(request).list_conversations(
            user, min(max(limit, 1), 200)
        )
    }


@router.get("/conversations/{conversation_id}")
async def get_jarvis_conversation(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("*")),
):
    return await _service(request).get_conversation(user, conversation_id)


@router.post("/commands")
async def execute_jarvis_command(
    body: JarvisCommandRequest,
    request: Request,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
    user: AuthenticatedUser = Depends(require_permission("*")),
):
    claim = await _claim(
        request, user, "/crm/jarvis/commands",
        idempotency_key, body.model_dump(mode="json"),
    )
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).run_command(
            user=user, message=body.message,
            conversation_id=body.conversation_id,
        )
        await complete_request(
            claim.event_id, response,
            session_factory=_factory(request),
        )
        return response
    except Exception as exc:
        await fail_request(
            claim.event_id, exc, session_factory=_factory(request)
        )
        raise


@router.get("/runs/{run_id}")
async def get_jarvis_run(
    run_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_permission("*")),
):
    return await _service(request).get_run(user, run_id)


@router.post("/actions/{action_id}/resume")
async def resume_approved_jarvis_action(
    action_id: str,
    body: JarvisActionResumeRequest,
    request: Request,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
    user: AuthenticatedUser = Depends(require_permission("*")),
):
    payload = {"action_id": action_id, **body.model_dump(mode="json")}
    claim = await _claim(
        request, user, f"/crm/jarvis/actions/{action_id}/resume",
        idempotency_key, payload,
    )
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).resume_action(
            user=user, action_id=action_id,
            idempotency_key=f"founder:{idempotency_key}",
        )
        await complete_request(
            claim.event_id, response, session_factory=_factory(request)
        )
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc, session_factory=_factory(request))
        raise
