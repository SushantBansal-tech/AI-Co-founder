from datetime import UTC, date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.crm.auth import (
    AuthenticatedUser,
    create_auth_session,
    normalize_user_email,
    require_permission,
    require_user,
    token_digest,
    verify_password,
)
from app.crm.schemas import (
    ActivityCreateRequest,
    AssignmentRequest,
    CRMApprovalRequest,
    CRMMatchDecisionRequest,
    CloseLostRequest,
    CRMCustomerNoteRequest,
    CustomerUpdateRequest,
    LeadUpdateRequest,
    LoginRequest,
    TaskCompletionRequest,
    TaskCreateRequest,
    TaskUpdateRequest,
    UserCreateRequest,
)
from app.crm.service import CRMService
from app.customers.customer_360 import get_customer_360
from app.customers.merge_service import resolve_customer_match_review
from app.database.models.customer import (
    Customer,
    CustomerMatchReview,
    CustomerMatchReviewStatus,
)
from app.database.models.crm import AuthSession, BusinessMembership, User
from app.events.service import record_business_event
from app.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    claim_request,
    complete_request,
    fail_request,
)


router = APIRouter(prefix="/crm", tags=["AI-native CRM"])


@router.get("/app", include_in_schema=False)
async def crm_application():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


def _service(request: Request) -> CRMService:
    service = getattr(request.app.state, "crm_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="CRM service is not initialized.")
    return service


def _session_factory(request: Request):
    factory = getattr(request.app.state, "session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="Database session is unavailable.")
    return factory


def _dashboard_service(request: Request):
    service = getattr(request.app.state, "dashboard_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="Dashboard service is unavailable.")
    return service


def _dashboard_period(request: Request, date_from: date | None, date_to: date | None):
    if date_from is None or date_to is None:
        default_from, default_to = _dashboard_service(request).default_period()
        date_from = date_from or default_from
        date_to = date_to or default_to
    if date_from > date_to or (date_to - date_from).days > 366:
        raise HTTPException(status_code=422, detail="Invalid dashboard date range.")
    return date_from, date_to


async def _claim(user, endpoint, key, payload, thread_id=None):
    try:
        return await claim_request(
            business_id=user.business_id,
            endpoint=endpoint,
            idempotency_key=key,
            payload=payload,
            thread_id=thread_id,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/auth/login")
async def login(body: LoginRequest, request: Request):
    normalized = normalize_user_email(body.email)
    async with _session_factory(request)() as session:
        row = (
            await session.execute(
                select(User, BusinessMembership)
                .join(BusinessMembership, BusinessMembership.user_id == User.id)
                .where(
                    User.normalized_email == normalized,
                    User.status == "active",
                    BusinessMembership.business_id == body.business_id,
                    BusinessMembership.status == "active",
                )
            )
        ).one_or_none()
        if row is None or not verify_password(body.password, row[0].password_hash):
            raise HTTPException(status_code=401, detail="Invalid email, password, or business.")
        user, membership = row
        token, auth_session = await create_auth_session(
            session, user_id=user.id, business_id=membership.business_id
        )
        await session.commit()
        return {
            "access_token": token,
            "token_type": "bearer",
            "expires_at": auth_session.expires_at,
            "user": {
                "id": user.id,
                "email": user.email,
                "display_name": user.display_name,
                "business_id": membership.business_id,
                "role": membership.role,
            },
        }


@router.post("/auth/logout")
async def logout(
    request: Request,
    authorization: str = Header(...),
    user: AuthenticatedUser = Depends(require_user),
):
    _, _, token = authorization.partition(" ")
    async with _session_factory(request)() as session:
        auth_session = await session.scalar(select(AuthSession).where(
            AuthSession.token_hash == token_digest(token),
            AuthSession.user_id == user.user_id,
            AuthSession.business_id == user.business_id,
            AuthSession.revoked_at.is_(None),
        ))
        if auth_session:
            auth_session.revoked_at = datetime.now(UTC).replace(tzinfo=None)
            await session.commit()
    return {"status": "logged_out"}


@router.get("/me")
async def me(user: AuthenticatedUser = Depends(require_user)):
    return {
        "user_id": user.user_id,
        "business_id": user.business_id,
        "membership_id": user.membership_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
    }


@router.get("/users")
async def list_users(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    return {"items": await _service(request).list_members(user)}


@router.post("/users")
async def create_user(
    body: UserCreateRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("*")),
):
    claim = await _claim(user, "/crm/users", idempotency_key, body.model_dump())
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).create_member(user, **body.model_dump())
        await complete_request(claim.event_id, response)
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.get("/customers")
async def list_customers(
    request: Request,
    search: str | None = Query(default=None, max_length=255),
    city: str | None = Query(default=None, max_length=100),
    owner_id: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_user),
):
    return await _service(request).list_customers(
        user, search=search, city=city, owner_id=owner_id,
        page=page, page_size=page_size,
    )


@router.get("/customer-match-reviews")
async def crm_match_reviews(
    request: Request,
    status: str = "pending",
    user: AuthenticatedUser = Depends(require_permission("customer_merge:resolve")),
):
    try:
        review_status = CustomerMatchReviewStatus(status)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Unsupported match-review status.") from exc
    async with _session_factory(request)() as session:
        rows = (await session.execute(
            select(CustomerMatchReview)
            .where(
                CustomerMatchReview.business_id == user.business_id,
                CustomerMatchReview.status == review_status,
            )
            .order_by(CustomerMatchReview.created_at)
        )).scalars().all()
        result = []
        for review in rows:
            provisional = await session.get(Customer, review.provisional_customer_id)
            candidate = await session.get(Customer, review.candidate_customer_id)
            result.append({
                "id": review.id,
                "lead_id": review.lead_id,
                "confidence": review.confidence,
                "matched_signals": review.matched_signals,
                "conflicting_signals": review.conflicting_signals,
                "status": getattr(review.status, "value", review.status),
                "provisional_customer": _service(request).customer_payload(provisional) if provisional else None,
                "candidate_customer": _service(request).customer_payload(candidate) if candidate else None,
                "created_at": review.created_at,
            })
    return {"items": result}


@router.post("/customer-match-reviews/{review_id}/resolve")
async def crm_resolve_match_review(
    review_id: str, body: CRMMatchDecisionRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("customer_merge:resolve")),
):
    claim = await _claim(
        user, f"/crm/customer-match-reviews/{review_id}/resolve",
        idempotency_key, body.model_dump(),
    )
    if claim.is_cached:
        return claim.cached_response
    try:
        async with _session_factory(request)() as session:
            review = await resolve_customer_match_review(
                session, review_id=review_id, business_id=user.business_id,
                action=body.action, resolved_by=user.user_id, notes=body.notes,
            )
            response = {
                "id": review.id,
                "status": getattr(review.status, "value", review.status),
                "resolved_by": review.resolved_by,
                "resolved_at": review.resolved_at,
            }
        await complete_request(claim.event_id, jsonable_encoder(response))
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.get("/customers/{customer_id}")
async def customer_detail(
    customer_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    customer = await _service(request).get_customer(user, customer_id)
    return _service(request).customer_payload(customer)


@router.patch("/customers/{customer_id}")
async def update_customer(
    customer_id: str, body: CustomerUpdateRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_user),
):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=422, detail="At least one field must be supplied.")
    claim = await _claim(user, f"/crm/customers/{customer_id}", idempotency_key, changes)
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).update_customer(user, customer_id, changes)
        await complete_request(claim.event_id, response)
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.post("/customers/{customer_id}/assign")
async def assign_customer(
    customer_id: str, body: AssignmentRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("customer:assign")),
):
    claim = await _claim(user, f"/crm/customers/{customer_id}/assign", idempotency_key, body.model_dump())
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).assign_customer(
            user, customer_id, body.user_id, body.reason
        )
        await complete_request(claim.event_id, response)
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.get("/customers/{customer_id}/360")
async def crm_customer_360(
    customer_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    customer = await _service(request).get_customer(user, customer_id)
    async with _session_factory(request)() as session:
        result = await get_customer_360(
            session, business_id=user.business_id, customer_id=customer.id
        )
    result["customer"]["account_owner_id"] = customer.account_owner_id
    result["open_tasks"] = (await _service(request).list_tasks(
        user, status=None, assigned_to=None, overdue=False, page=1, page_size=100
    ))["items"]
    result["open_tasks"] = [
        item for item in result["open_tasks"]
        if item["customer_id"] == customer_id and item["status"] in {"open", "in_progress"}
    ]
    return result


@router.get("/customers/{customer_id}/sales-context")
async def crm_customer_sales_context(
    customer_id: str,
    request: Request,
    agent_name: str = Query(default="customer_qualification", min_length=1, max_length=100),
    query: str | None = Query(default=None, max_length=2000),
    top_k: int = Query(default=5, ge=1, le=20),
    user: AuthenticatedUser = Depends(require_user),
):
    await _service(request).get_customer(user, customer_id)
    context_service = getattr(request.app.state, "sales_context_service", None)
    if context_service is None:
        raise HTTPException(status_code=503, detail="Sales context service is unavailable.")
    context = await context_service.get_context(
        business_id=user.business_id,
        customer_id=customer_id,
        agent_name=agent_name,
        query=query,
        top_k=top_k,
    )
    return context.model_dump(mode="json")


@router.get("/customers/{customer_id}/{resource}")
async def customer_related(
    customer_id: str,
    resource: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    allowed = {
        "interactions", "quotations", "orders", "payments",
        "pipelines", "followups", "purchase_orders",
    }
    if resource not in allowed:
        raise HTTPException(status_code=404, detail="Unknown customer resource.")
    return {"items": await _service(request).customer_related(user, customer_id, resource)}


@router.post("/customers/{customer_id}/notes")
async def create_customer_note(
    customer_id: str, body: CRMCustomerNoteRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_user),
):
    await _service(request).get_customer(user, customer_id)
    memory_service = getattr(request.app.state, "customer_memory_service", None)
    if memory_service is None:
        raise HTTPException(status_code=503, detail="Customer memory service is unavailable.")
    claim = await _claim(
        user, f"/crm/customers/{customer_id}/notes", idempotency_key,
        body.model_dump(), thread_id=body.thread_id,
    )
    if claim.is_cached:
        return claim.cached_response
    try:
        note_id, outbox_id = await memory_service.create_note(
            business_id=user.business_id, customer_id=customer_id,
            content=body.content, content_type=body.content_type,
            thread_id=body.thread_id, interaction_id=body.interaction_id,
            created_by=user.user_id, request_event_id=claim.event_id,
        )
        response = {"note_id": note_id, "memory_outbox_id": outbox_id, "memory_status": "queued"}
        await complete_request(claim.event_id, response, thread_id=body.thread_id)
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.get("/leads")
async def list_leads(
    request: Request,
    status: str | None = None,
    source: str | None = None,
    assigned_to: str | None = None,
    pipeline_status: str | None = None,
    unassigned: bool = False,
    overdue: bool = False,
    search: str | None = Query(default=None, max_length=255),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_user),
):
    return await _service(request).list_leads(
        user, status=status, source=source, assigned_to=assigned_to,
        pipeline_status=pipeline_status, unassigned=unassigned,
        overdue=overdue, search=search, page=page, page_size=page_size,
    )


@router.get("/leads/{lead_id}")
async def lead_detail(
    lead_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    return await _service(request).get_lead_payload(user, lead_id)


@router.patch("/leads/{lead_id}")
async def update_lead(
    lead_id: str, body: LeadUpdateRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_user),
):
    changes = body.model_dump(exclude_unset=True)
    claim = await _claim(user, f"/crm/leads/{lead_id}", idempotency_key, changes)
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).update_lead(user, lead_id, changes)
        await complete_request(claim.event_id, response, thread_id=response["thread_id"])
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.post("/leads/{lead_id}/assign")
async def assign_lead(
    lead_id: str, body: AssignmentRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("lead:assign")),
):
    claim = await _claim(user, f"/crm/leads/{lead_id}/assign", idempotency_key, body.model_dump())
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).assign_lead(user, lead_id, body.user_id, body.reason)
        await complete_request(claim.event_id, response, thread_id=response["thread_id"])
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.post("/leads/{lead_id}/close-lost")
async def close_lost(
    lead_id: str, body: CloseLostRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("lead:close")),
):
    claim = await _claim(user, f"/crm/leads/{lead_id}/close-lost", idempotency_key, body.model_dump(mode="json"))
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).close_lost(user, lead_id, **body.model_dump())
        graph = getattr(request.app.state, "sales_graph", None)
        if graph is not None:
            await graph.aupdate_state(
                {"configurable": {"thread_id": response["thread_id"]}},
                {"pipeline_status": "closed_lost", "waiting_for": "none", "status_reason": body.reason_code},
            )
        await complete_request(claim.event_id, response, thread_id=response["thread_id"])
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.post("/leads/{lead_id}/reopen")
async def reopen_lead(
    lead_id: str, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("lead:reopen")),
):
    claim = await _claim(user, f"/crm/leads/{lead_id}/reopen", idempotency_key, {})
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).reopen_lead(user, lead_id)
        graph = getattr(request.app.state, "sales_graph", None)
        if graph is not None:
            await graph.aupdate_state(
                {"configurable": {"thread_id": response["thread_id"]}},
                {"pipeline_status": "processing", "waiting_for": "none", "status_reason": "Lead reopened by sales manager."},
            )
        await complete_request(claim.event_id, response, thread_id=response["thread_id"])
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.get("/leads/{lead_id}/timeline")
async def lead_timeline(
    lead_id: str, request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    return {"items": await _service(request).timeline(user, lead_id)}


@router.get("/tasks")
async def list_tasks(
    request: Request,
    status: str | None = None,
    assigned_to: str | None = None,
    overdue: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_user),
):
    return await _service(request).list_tasks(
        user, status=status, assigned_to=assigned_to,
        overdue=overdue, page=page, page_size=page_size,
    )


@router.post("/tasks")
async def create_task(
    body: TaskCreateRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("task:create")),
):
    claim = await _claim(user, "/crm/tasks", idempotency_key, body.model_dump(mode="json"), thread_id=body.thread_id)
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).create_task(user, body.model_dump())
        await complete_request(claim.event_id, response, thread_id=body.thread_id)
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.patch("/tasks/{task_id}")
async def update_task(
    task_id: str, body: TaskUpdateRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_user),
):
    changes = body.model_dump(exclude={"version"}, exclude_unset=True)
    claim = await _claim(user, f"/crm/tasks/{task_id}", idempotency_key, body.model_dump(mode="json"))
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).update_task(user, task_id, changes, body.version)
        await complete_request(claim.event_id, response, thread_id=response.get("thread_id"))
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: str, body: TaskCompletionRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_user),
):
    claim = await _claim(user, f"/crm/tasks/{task_id}/complete", idempotency_key, body.model_dump())
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).finish_task(
            user, task_id, status="completed", notes=body.completion_notes,
            expected_version=body.version,
        )
        await complete_request(claim.event_id, response, thread_id=response.get("thread_id"))
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.post("/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str, body: TaskCompletionRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_user),
):
    claim = await _claim(user, f"/crm/tasks/{task_id}/cancel", idempotency_key, body.model_dump())
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).finish_task(
            user, task_id, status="cancelled", notes=body.completion_notes,
            expected_version=body.version,
        )
        await complete_request(claim.event_id, response, thread_id=response.get("thread_id"))
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.post("/activities")
async def create_activity(
    body: ActivityCreateRequest, request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_permission("activity:create")),
):
    claim = await _claim(user, "/crm/activities", idempotency_key, body.model_dump(mode="json"), thread_id=body.thread_id)
    if claim.is_cached:
        return claim.cached_response
    try:
        response = await _service(request).create_activity(user, body.model_dump())
        await complete_request(claim.event_id, response, thread_id=body.thread_id)
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.get("/pipeline")
async def crm_pipeline(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    return {"items": await _service(request).pipeline_cards(user)}


@router.get("/approvals")
async def crm_approvals(
    request: Request,
    user: AuthenticatedUser = Depends(require_user),
):
    return {"items": await _service(request).approvals(user)}


@router.post("/approvals/{thread_id}/approve")
async def approve_pipeline(
    thread_id: str,
    body: CRMApprovalRequest,
    request: Request,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=200),
    user: AuthenticatedUser = Depends(require_user),
):
    permission_by_stage = {
        "qualification": "approval:finance",
        "requirement": "approval:sales",
        "feasibility": "approval:production",
        "pricing": "approval:finance",
        "negotiation": "approval:sales",
        "po": "approval:production",
        "po_revalidation": "approval:production",
    }
    required_permission = permission_by_stage[body.approved_stage]
    if not user.has_permission(required_permission):
        raise HTTPException(
            status_code=403,
            detail=f"Role '{user.role}' cannot approve stage '{body.approved_stage}'.",
        )
    graph = getattr(request.app.state, "sales_graph", None)
    if graph is None:
        raise HTTPException(status_code=503, detail="Sales graph is unavailable.")
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = await graph.aget_state(config)
    current = dict(snapshot.values or {}) if snapshot else {}
    if current.get("business_id") != user.business_id:
        raise HTTPException(status_code=404, detail="Pipeline not found.")
    current_status = str(current.get("pipeline_status") or "")
    if (
        current_status not in {"awaiting_approval", f"awaiting_approval:{body.approved_stage}"}
        or current.get("human_approval_stage") != body.approved_stage
    ):
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Pipeline is not waiting for the requested approval.",
                "pipeline_status": current.get("pipeline_status"),
                "pending_stage": current.get("human_approval_stage"),
            },
        )
    if body.approved_stage == "pricing":
        pricing = current.get("pricing") or {}
        missing = ((pricing.get("price_logic") or {}).get("validation") or {}).get("missing_inputs", [])
        if not pricing.get("pricing_possible", False) or missing:
            raise HTTPException(
                status_code=409,
                detail={"message": "Fundamental pricing data cannot be overridden by approval.", "missing_inputs": missing},
            )
    claim = await _claim(
        user, "/crm/approvals/approve", idempotency_key,
        {"thread_id": thread_id, **body.model_dump()}, thread_id=thread_id,
    )
    if claim.is_cached:
        return claim.cached_response
    try:
        result = await graph.ainvoke(
            {
                "business_id": user.business_id,
                "trigger": "approved",
                "approved_stage": body.approved_stage,
                "needs_human_approval": False,
                "human_approval_stage": None,
                "error": None,
            },
            config=config,
        )
        if result.get("error") and result.get("pipeline_status") == "failed":
            raise RuntimeError(result["error"])
        response = {
            "thread_id": thread_id,
            "approved_stage": body.approved_stage,
            "approved_by": user.user_id,
            "state": result,
        }
        async with _session_factory(request)() as session:
            await record_business_event(
                session,
                business_id=user.business_id,
                customer_id=current.get("customer_id"),
                lead_id=current.get("lead_id"),
                thread_id=thread_id,
                event_type="approval.granted",
                source="crm",
                actor_type="employee",
                actor_id=user.user_id,
                entity_type="pipeline",
                entity_id=thread_id,
                data={"stage": body.approved_stage, "role": user.role},
            )
            await session.commit()
        encoded = jsonable_encoder(response)
        await complete_request(claim.event_id, encoded, thread_id=thread_id)
        return encoded
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise


@router.get("/dashboard/overview")
async def crm_dashboard_overview(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    risk_after_days: int = Query(default=7, ge=0, le=365),
    user: AuthenticatedUser = Depends(require_user),
):
    date_from, date_to = _dashboard_period(request, date_from, date_to)
    return await _dashboard_service(request).overview(
        business_id=user.business_id,
        date_from=date_from,
        date_to=date_to,
        risk_after_days=risk_after_days,
    )


@router.get("/dashboard/trends")
async def crm_dashboard_trends(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    user: AuthenticatedUser = Depends(require_user),
):
    date_from, date_to = _dashboard_period(request, date_from, date_to)
    return await _dashboard_service(request).trends(
        business_id=user.business_id, date_from=date_from, date_to=date_to
    )


@router.get("/dashboard/channels")
async def crm_dashboard_channels(
    request: Request,
    date_from: date | None = None,
    date_to: date | None = None,
    user: AuthenticatedUser = Depends(require_user),
):
    date_from, date_to = _dashboard_period(request, date_from, date_to)
    return await _dashboard_service(request).channels(
        business_id=user.business_id, date_from=date_from, date_to=date_to
    )


@router.get("/dashboard/attention")
async def crm_dashboard_attention(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    user: AuthenticatedUser = Depends(require_user),
):
    return await _dashboard_service(request).attention(
        business_id=user.business_id, limit=limit
    )
