from datetime import UTC, datetime
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.authority.auth import AuthenticatedAIPrincipal
from app.authority.service import AuthorityService
from app.business_tools.context import ToolContext
from app.business_tools.handlers import BusinessToolHandlers
from app.business_tools.registry import BusinessToolRegistry
from app.business_tools.schemas import ToolExecutionResponse
from app.database.models.business_tool import AIToolExecution
from app.events.service import record_business_event
from app.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    claim_request,
    complete_request,
    fail_request,
    hash_request,
)


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class BusinessToolExecutor:
    def __init__(self, session_factory, authority_service: AuthorityService):
        self.session_factory = session_factory
        self.authority_service = authority_service
        self.registry = BusinessToolRegistry(BusinessToolHandlers(session_factory))

    async def catalog(self, principal: AuthenticatedAIPrincipal) -> list[dict]:
        return [item for item in self.registry.public_catalog()
                if item["required_scope"] in principal.scopes]

    async def _create_execution(self, principal, tool, arguments, key) -> AIToolExecution:
        async with self.session_factory() as session:
            execution = None
            if key is not None:
                execution = await session.scalar(select(AIToolExecution).where(
                    AIToolExecution.business_id == principal.business_id,
                    AIToolExecution.principal_id == principal.principal_id,
                    AIToolExecution.tool_name == tool.name,
                    AIToolExecution.idempotency_key == key,
                ).with_for_update())
            if execution is None:
                execution = AIToolExecution(
                    id=str(uuid4()), business_id=principal.business_id,
                    principal_id=principal.principal_id, tool_name=tool.name,
                    risk_level=tool.risk_level, required_scope=tool.required_scope,
                    is_mutation=tool.is_mutation, input_hash=hash_request(arguments),
                    idempotency_key=key,
                )
            else:
                execution.status = "processing"
                execution.authority_decision = None
                execution.response_json = None
                execution.error_code = None
                execution.error_message = None
                execution.entity_type = None
                execution.entity_id = None
                execution.completed_at = None
                execution.started_at = utc_now()
            session.add(execution)
            await session.commit()
        return execution

    async def _finish_execution(
        self, execution_id: str, *, status: str, decision: str | None = None,
        response: dict | None = None, error_code: str | None = None,
        error_message: str | None = None, entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> None:
        async with self.session_factory() as session:
            row = await session.get(AIToolExecution, execution_id)
            if row:
                row.status = status
                row.authority_decision = decision
                row.response_json = response
                row.error_code = error_code
                row.error_message = error_message
                row.entity_type = entity_type
                row.entity_id = entity_id
                row.completed_at = utc_now()
                await session.commit()

    async def execute(
        self, *, principal: AuthenticatedAIPrincipal, tool_name: str,
        raw_arguments: dict, idempotency_key: str | None,
    ) -> dict:
        tool = self.registry.require(tool_name)
        if tool.required_scope not in principal.scopes:
            raise HTTPException(
                status_code=403,
                detail=f"AI principal lacks required scope '{tool.required_scope}'.",
            )
        try:
            arguments = tool.input_model.model_validate(raw_arguments)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=exc.errors()) from exc
        if tool.requires_idempotency and not idempotency_key:
            raise HTTPException(
                status_code=400,
                detail=f"Idempotency-Key is required for mutation tool '{tool.name}'.",
            )

        claim = None
        endpoint = f"/ai/tools/{principal.principal_id}/{tool.name}/execute"
        if tool.requires_idempotency:
            try:
                claim = await claim_request(
                    business_id=principal.business_id, endpoint=endpoint,
                    idempotency_key=idempotency_key or "", payload=raw_arguments,
                    session_factory=self.session_factory,
                )
            except (IdempotencyConflict, IdempotencyInProgress) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            if claim.is_cached:
                return claim.cached_response or {}

        execution = await self._create_execution(
            principal, tool, raw_arguments,
            idempotency_key if tool.requires_idempotency else None,
        )
        context = ToolContext.from_principal(
            principal, execution_id=execution.id,
            idempotency_key=idempotency_key,
        )
        decision = "SCOPE_ALLOWED"
        try:
            if tool.is_mutation:
                authority_result = await self.authority_service.evaluate_action(
                    business_id=principal.business_id,
                    principal_id=principal.principal_id,
                    action_type=tool.authority_action or tool.name,
                    facts=arguments.model_dump(mode="json"),
                    tool_execution_id=execution.id,
                )
                authority = authority_result.model_dump(mode="json")
                decision = authority_result.decision.value
                if decision != "ALLOW":
                    status_by_decision = {
                        "REQUIRE_APPROVAL": "pending_approval",
                        "REQUIRE_MORE_INFORMATION": "needs_information",
                        "BLOCKED_MASTER_DATA": "blocked_master_data",
                    }
                    response_status = status_by_decision.get(decision, "denied")
                    response = ToolExecutionResponse(
                        execution_id=execution.id, tool_name=tool.name,
                        status=response_status, authority_decision=decision,
                        reason="; ".join(authority_result.reasons), result=None,
                        authority=authority,
                    ).model_dump(mode="json")
                    await self._finish_execution(
                        execution.id, status=response_status, decision=decision,
                        response=response,
                        error_code=(
                            "authority_denied" if decision == "DENY"
                            else "authority_action_not_executed"
                        ),
                        error_message="; ".join(authority_result.reasons),
                    )
                    if claim:
                        await complete_request(
                            claim.event_id, response,
                            session_factory=self.session_factory,
                        )
                    return response

            raw_result = await tool.handler(context, arguments)
            result = tool.output_model.model_validate(raw_result).model_dump(mode="json")
            entity_id = (
                str(result.get(tool.entity_id_field))
                if tool.entity_id_field and result.get(tool.entity_id_field) else None
            )
            response = ToolExecutionResponse(
                execution_id=execution.id, tool_name=tool.name,
                status="completed", authority_decision=decision,
                result=result,
                authority=(authority if tool.is_mutation else None),
            ).model_dump(mode="json")
            await self._finish_execution(
                execution.id, status="completed", decision=decision,
                response=response, entity_type=tool.entity_type,
                entity_id=entity_id,
            )
            if claim:
                await complete_request(
                    claim.event_id, response,
                    session_factory=self.session_factory,
                )
            return response
        except Exception as exc:
            await self._finish_execution(
                execution.id, status="failed", decision=decision,
                error_code=type(exc).__name__, error_message=str(exc),
            )
            if claim:
                await fail_request(
                    claim.event_id, exc, session_factory=self.session_factory
                )
            raise
