import json
import os
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from app.authority.auth import AuthenticatedAIPrincipal
from app.business_tools.executor import BusinessToolExecutor
from app.crm.auth import AuthenticatedUser
from app.database.models.authority import (
    AIPrincipalScope,
    AIServicePrincipal,
    AuthorityApprovalRequest,
    BusinessSettings,
)
from app.database.models.jarvis import JarvisConversation, JarvisMessage, JarvisRun
from app.events.service import record_business_event
from app.jarvis.planner import JarvisPlanner
from app.jarvis.schemas import JarvisCommandResponse, JarvisPlan


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def iso(value) -> str | None:
    return value.isoformat() if value else None


class JarvisService:
    """Founder-facing orchestrator constrained to registered business tools."""

    def __init__(
        self, session_factory, tool_executor: BusinessToolExecutor,
        planner: JarvisPlanner,
    ):
        self.session_factory = session_factory
        self.tool_executor = tool_executor
        self.planner = planner

    async def _principal(self, business_id: str) -> AuthenticatedAIPrincipal:
        async with self.session_factory() as session:
            query = select(AIServicePrincipal).where(
                AIServicePrincipal.business_id == business_id,
                AIServicePrincipal.status == "active",
                AIServicePrincipal.revoked_at.is_(None),
            )
            configured_name = os.getenv("JARVIS_PRINCIPAL_NAME")
            if configured_name:
                query = query.where(AIServicePrincipal.name == configured_name)
            principals = (await session.scalars(
                query.order_by(AIServicePrincipal.created_at)
            )).all()
            if not principals:
                raise HTTPException(
                    status_code=409,
                    detail="No active Jarvis AI service principal is configured for this business.",
                )
            if len(principals) > 1 and not configured_name:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Multiple active AI principals exist. Set JARVIS_PRINCIPAL_NAME "
                        "to choose the principal used by the orchestrator."
                    ),
                )
            principal = principals[0]
            scopes = frozenset((await session.scalars(select(AIPrincipalScope.scope).where(
                AIPrincipalScope.business_id == business_id,
                AIPrincipalScope.principal_id == principal.id,
                AIPrincipalScope.revoked_at.is_(None),
            ))).all())
        return AuthenticatedAIPrincipal(
            principal_id=principal.id, business_id=business_id,
            name=principal.name, scopes=scopes,
        )

    async def create_conversation(self, user: AuthenticatedUser, title: str) -> dict:
        async with self.session_factory() as session:
            row = JarvisConversation(
                business_id=user.business_id,
                created_by_user_id=user.user_id,
                title=title.strip(),
            )
            session.add(row)
            await record_business_event(
                session, business_id=user.business_id,
                event_type="jarvis.conversation_created", source="crm",
                actor_type="human", actor_id=user.user_id,
                entity_type="jarvis_conversation", entity_id=row.id,
                data={"title": row.title},
            )
            await session.commit()
            return self.conversation_payload(row)

    @staticmethod
    def conversation_payload(row: JarvisConversation) -> dict:
        return {
            "id": row.id, "business_id": row.business_id,
            "created_by_user_id": row.created_by_user_id,
            "title": row.title, "status": row.status,
            "created_at": iso(row.created_at), "updated_at": iso(row.updated_at),
        }

    async def _get_or_create_conversation(
        self, user: AuthenticatedUser, conversation_id: str | None, message: str,
    ) -> JarvisConversation:
        async with self.session_factory() as session:
            if conversation_id:
                row = await session.scalar(select(JarvisConversation).where(
                    JarvisConversation.id == conversation_id,
                    JarvisConversation.business_id == user.business_id,
                    JarvisConversation.created_by_user_id == user.user_id,
                    JarvisConversation.status == "active",
                ))
                if row is None:
                    raise HTTPException(status_code=404, detail="Jarvis conversation was not found.")
                return row
            title = " ".join(message.strip().split())[:120]
            row = JarvisConversation(
                business_id=user.business_id,
                created_by_user_id=user.user_id,
                title=title or "New Jarvis conversation",
            )
            session.add(row)
            await session.commit()
            return row

    async def _create_run(
        self, *, user: AuthenticatedUser, principal: AuthenticatedAIPrincipal,
        conversation: JarvisConversation, message: str,
    ) -> tuple[JarvisMessage, JarvisRun]:
        async with self.session_factory() as session:
            input_message = JarvisMessage(
                business_id=user.business_id,
                conversation_id=conversation.id,
                role="user", content=message,
            )
            session.add(input_message)
            await session.flush()
            run = JarvisRun(
                business_id=user.business_id,
                conversation_id=conversation.id,
                requested_by_user_id=user.user_id,
                principal_id=principal.principal_id,
                model=self.planner.model_name,
                status="PLANNING",
                input_message_id=input_message.id,
            )
            session.add(run)
            conversation_row = await session.get(JarvisConversation, conversation.id)
            conversation_row.updated_at = utc_now()
            await session.commit()
            return input_message, run

    async def _context(
        self, *, user: AuthenticatedUser, principal: AuthenticatedAIPrincipal,
        conversation_id: str,
    ) -> dict:
        async with self.session_factory() as session:
            settings = await session.get(BusinessSettings, user.business_id)
            approvals = (await session.scalars(select(AuthorityApprovalRequest).where(
                AuthorityApprovalRequest.business_id == user.business_id,
                AuthorityApprovalRequest.status == "PENDING",
            ).order_by(AuthorityApprovalRequest.created_at.desc()).limit(20))).all()
            messages = (await session.scalars(select(JarvisMessage).where(
                JarvisMessage.business_id == user.business_id,
                JarvisMessage.conversation_id == conversation_id,
            ).order_by(JarvisMessage.created_at.desc()).limit(12))).all()
        business_controls = {
            "ai_operating_mode": settings.ai_operating_mode if settings else None,
            "maximum_automatic_discount_pct": (
                str(settings.maximum_automatic_discount_pct) if settings else None
            ),
            "maximum_automatic_quotation_value": (
                str(settings.maximum_automatic_quotation_value) if settings else None
            ),
            "minimum_margin_pct": str(settings.minimum_margin_pct) if settings else None,
            "settings_version": settings.version if settings else None,
            "authenticated_user_role": user.role,
        }
        return {
            "business_controls": business_controls,
            "available_tools": await self.tool_executor.catalog(principal),
            "pending_approvals": [{
                "approval_request_id": row.id,
                "action_request_id": row.action_request_id,
                "action_type": row.action_type,
                "required_role": row.required_role,
                "reason": row.reason,
            } for row in approvals],
            "recent_messages": [{
                "role": row.role, "content": row.content[:2000]
            } for row in reversed(messages)],
        }

    def _validate_plan(
        self, plan: JarvisPlan, principal: AuthenticatedAIPrincipal,
    ) -> None:
        for call in plan.tool_calls:
            tool = self.tool_executor.registry.require(call.tool_name)
            if tool.required_scope not in principal.scopes:
                raise HTTPException(
                    status_code=403,
                    detail=f"Jarvis lacks scope '{tool.required_scope}' for '{tool.name}'.",
                )
            try:
                tool.input_model.model_validate(call.arguments)
            except ValidationError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": f"Jarvis proposed invalid arguments for '{tool.name}'.",
                        "errors": exc.errors(),
                    },
                ) from exc

    @staticmethod
    def _supporting_references(tool_name: str, response: dict) -> list[dict]:
        refs = []
        if response.get("action_request_id"):
            refs.append({
                "entity_type": "ai_action_request",
                "entity_id": response["action_request_id"],
                "source": "postgresql",
                "tool_name": tool_name,
            })
        if response.get("execution_id"):
            refs.append({
                "entity_type": "ai_tool_execution",
                "entity_id": response["execution_id"],
                "source": "postgresql",
                "tool_name": tool_name,
            })
        result = response.get("result") or {}
        if tool_name == "get_customer_sales_context" and isinstance(result, dict):
            if result.get("customer_id"):
                refs.append({
                    "entity_type": "customer",
                    "entity_id": str(result["customer_id"]),
                    "source": "postgresql",
                    "tool_name": tool_name,
                })
            for memory in result.get("semantic_memories") or []:
                if isinstance(memory, dict) and memory.get("memory_id"):
                    refs.append({
                        "entity_type": "customer_memory",
                        "entity_id": str(memory["memory_id"]),
                        "source": "qdrant",
                        "tool_name": tool_name,
                    })
        records = result.get("items") if isinstance(result, dict) else None
        if not isinstance(records, list):
            records = [result] if isinstance(result, dict) else []
        for record in records[:50]:
            if not isinstance(record, dict):
                continue
            for key, value in record.items():
                if value and (key == "id" or key.endswith("_id")):
                    refs.append({
                        "entity_type": key.removesuffix("_id") if key != "id" else tool_name,
                        "entity_id": str(value), "source": "postgresql",
                        "tool_name": tool_name,
                    })
        unique = {}
        for item in refs:
            unique[(item["entity_type"], item["entity_id"], item["tool_name"])] = item
        return list(unique.values())

    async def _persist_tool_message(
        self, *, business_id: str, conversation_id: str,
        call_id: str, response: dict, supporting: list[dict],
    ) -> None:
        async with self.session_factory() as session:
            session.add(JarvisMessage(
                business_id=business_id, conversation_id=conversation_id,
                role="tool", content=json.dumps(response, default=str),
                tool_call_id=call_id,
                action_request_id=response.get("action_request_id"),
                supporting_data=supporting,
            ))
            await session.commit()

    async def _complete(
        self, *, run_id: str, conversation_id: str, business_id: str,
        user_id: str, status: str, answer: str, plan: JarvisPlan,
        tool_results: list[dict], supporting_data: list[dict],
    ) -> dict:
        async with self.session_factory() as session:
            run = await session.scalar(select(JarvisRun).where(
                JarvisRun.id == run_id, JarvisRun.business_id == business_id,
            ).with_for_update())
            final_message = JarvisMessage(
                business_id=business_id, conversation_id=conversation_id,
                role="assistant", content=answer,
                supporting_data=supporting_data,
            )
            session.add(final_message)
            await session.flush()
            run.status = status
            run.plan_json = plan.model_dump(mode="json")
            run.tool_results_json = tool_results
            run.supporting_data = supporting_data
            run.final_message_id = final_message.id
            run.completed_at = utc_now()
            conversation = await session.get(JarvisConversation, conversation_id)
            conversation.updated_at = utc_now()
            await record_business_event(
                session, business_id=business_id,
                event_type="jarvis.run_completed", source="jarvis",
                actor_type="human", actor_id=user_id,
                entity_type="jarvis_run", entity_id=run.id,
                data={"status": status, "tool_call_count": len(tool_results)},
            )
            await session.commit()

        public_status = {
            "COMPLETED": "completed",
            "AWAITING_APPROVAL": "awaiting_approval",
            "BLOCKED": "blocked",
            "FAILED": "failed",
        }[status]
        action_requests = [{
            "action_request_id": item["response"].get("action_request_id"),
            "approval_request_id": (item["response"].get("authority") or {}).get(
                "approval_request_id"
            ),
            "status": item["response"].get("status"),
            "tool_name": item["tool_name"],
            "reason": item["response"].get("reason"),
        } for item in tool_results if item["response"].get("action_request_id")]
        return JarvisCommandResponse(
            conversation_id=conversation_id, run_id=run_id,
            status=public_status, answer=answer,
            action_requests=action_requests,
            supporting_data=supporting_data,
        ).model_dump(mode="json")

    async def _fail_run(self, run_id: str, exc: Exception) -> None:
        async with self.session_factory() as session:
            run = await session.get(JarvisRun, run_id)
            if run:
                run.status = "FAILED"
                run.error_code = type(exc).__name__
                run.error_message = str(exc)[:5000]
                run.completed_at = utc_now()
                await session.commit()

    @staticmethod
    def _fallback_answer(tool_results: list[dict]) -> str:
        if not tool_results:
            return "I could not verify any CRM data for this request."
        pending = [item for item in tool_results
                   if item["response"].get("status") == "pending_approval"]
        blocked = [item for item in tool_results if item["response"].get("status") in {
            "denied", "needs_information", "blocked_master_data"
        }]
        completed = [item for item in tool_results
                     if item["response"].get("status") == "completed"]
        parts = []
        if completed:
            parts.append("Completed: " + ", ".join(item["tool_name"] for item in completed) + ".")
        if pending:
            parts.append("Awaiting approval: " + ", ".join(
                item["tool_name"] for item in pending
            ) + ".")
        if blocked:
            details = []
            for item in blocked:
                reason = item["response"].get("reason") or item["response"].get("status")
                details.append(f"{item['tool_name']} ({reason})")
            parts.append("Not executed: " + "; ".join(details) + ".")
        return " ".join(parts) or "The controlled tools returned no executable result."

    async def run_command(
        self, *, user: AuthenticatedUser, message: str,
        conversation_id: str | None,
    ) -> dict:
        principal = await self._principal(user.business_id)
        conversation = await self._get_or_create_conversation(
            user, conversation_id, message
        )
        _, run = await self._create_run(
            user=user, principal=principal,
            conversation=conversation, message=message,
        )
        try:
            context = await self._context(
                user=user, principal=principal,
                conversation_id=conversation.id,
            )
            plan = await self.planner.plan(message=message, context=context)
            self._validate_plan(plan, principal)
            if plan.needs_clarification:
                question = plan.clarification_question or (
                    "Please provide the missing customer, lead, or pipeline details."
                )
                return await self._complete(
                    run_id=run.id, conversation_id=conversation.id,
                    business_id=user.business_id, user_id=user.user_id,
                    status="COMPLETED", answer=question, plan=plan,
                    tool_results=[], supporting_data=[],
                )

            tool_results: list[dict] = []
            supporting_data: list[dict] = []
            for call in plan.tool_calls:
                response = await self.tool_executor.execute(
                    principal=principal, tool_name=call.tool_name,
                    raw_arguments=call.arguments,
                    idempotency_key=f"jarvis:{run.id}:{call.call_id}",
                    reason=call.reason,
                )
                wrapped = {
                    "call_id": call.call_id,
                    "tool_name": call.tool_name,
                    "response": response,
                }
                tool_results.append(wrapped)
                refs = self._supporting_references(call.tool_name, response)
                supporting_data.extend(refs)
                await self._persist_tool_message(
                    business_id=user.business_id,
                    conversation_id=conversation.id,
                    call_id=call.call_id, response=response, supporting=refs,
                )

            statuses = {item["response"].get("status") for item in tool_results}
            if "pending_approval" in statuses:
                run_status = "AWAITING_APPROVAL"
            elif statuses & {"denied", "needs_information", "blocked_master_data"}:
                run_status = "BLOCKED"
            else:
                run_status = "COMPLETED"
            try:
                grounded = await self.planner.answer(
                    message=message, plan=plan, tool_results=tool_results,
                    supporting_data=supporting_data,
                )
                answer = grounded.answer
            except Exception:
                # Tool and authority results are already durable. A temporary
                # LLM communication failure must not hide an approval or repeat
                # a completed business action.
                answer = self._fallback_answer(tool_results)
            return await self._complete(
                run_id=run.id, conversation_id=conversation.id,
                business_id=user.business_id, user_id=user.user_id,
                status=run_status, answer=answer, plan=plan,
                tool_results=tool_results, supporting_data=supporting_data,
            )
        except Exception as exc:
            await self._fail_run(run.id, exc)
            if isinstance(exc, HTTPException):
                raise
            raise HTTPException(
                status_code=502,
                detail=f"Jarvis planning failed: {type(exc).__name__}",
            ) from exc

    async def resume_action(
        self, *, user: AuthenticatedUser, action_id: str,
        idempotency_key: str,
    ) -> dict:
        """Founder-safe resume; the private AI credential never leaves the server."""
        principal = await self._principal(user.business_id)
        response = await self.tool_executor.resume(
            principal=principal, action_id=action_id,
            idempotency_key=idempotency_key,
        )
        async with self.session_factory() as session:
            await record_business_event(
                session, business_id=user.business_id,
                event_type="jarvis.approved_action_resumed", source="crm",
                actor_type="human", actor_id=user.user_id,
                entity_type="ai_action_request", entity_id=action_id,
                data={
                    "status": response.get("status"),
                    "execution_id": response.get("execution_id"),
                },
            )
            await session.commit()
        return response

    async def list_conversations(self, user: AuthenticatedUser, limit: int) -> list[dict]:
        async with self.session_factory() as session:
            rows = (await session.scalars(select(JarvisConversation).where(
                JarvisConversation.business_id == user.business_id,
                JarvisConversation.created_by_user_id == user.user_id,
            ).order_by(JarvisConversation.updated_at.desc()).limit(limit))).all()
        return [self.conversation_payload(row) for row in rows]

    async def get_conversation(self, user: AuthenticatedUser, conversation_id: str) -> dict:
        async with self.session_factory() as session:
            row = await session.scalar(select(JarvisConversation).where(
                JarvisConversation.id == conversation_id,
                JarvisConversation.business_id == user.business_id,
                JarvisConversation.created_by_user_id == user.user_id,
            ))
            if row is None:
                raise HTTPException(status_code=404, detail="Jarvis conversation was not found.")
            messages = (await session.scalars(select(JarvisMessage).where(
                JarvisMessage.business_id == user.business_id,
                JarvisMessage.conversation_id == conversation_id,
            ).order_by(JarvisMessage.created_at))).all()
            runs = (await session.scalars(select(JarvisRun).where(
                JarvisRun.business_id == user.business_id,
                JarvisRun.conversation_id == conversation_id,
            ).order_by(JarvisRun.started_at))).all()
        payload = self.conversation_payload(row)
        payload["messages"] = [{
            "id": item.id, "role": item.role, "content": item.content,
            "tool_call_id": item.tool_call_id,
            "action_request_id": item.action_request_id,
            "supporting_data": item.supporting_data,
            "created_at": iso(item.created_at),
        } for item in messages]
        payload["runs"] = [self.run_payload(item) for item in runs]
        return payload

    @staticmethod
    def run_payload(row: JarvisRun) -> dict:
        return {
            "id": row.id, "conversation_id": row.conversation_id,
            "status": row.status, "model": row.model,
            "principal_id": row.principal_id,
            "input_message_id": row.input_message_id,
            "final_message_id": row.final_message_id,
            "plan": row.plan_json,
            "tool_results": row.tool_results_json,
            "supporting_data": row.supporting_data,
            "error_code": row.error_code, "error_message": row.error_message,
            "started_at": iso(row.started_at), "completed_at": iso(row.completed_at),
        }

    async def get_run(self, user: AuthenticatedUser, run_id: str) -> dict:
        async with self.session_factory() as session:
            row = await session.scalar(select(JarvisRun).where(
                JarvisRun.id == run_id,
                JarvisRun.business_id == user.business_id,
                JarvisRun.requested_by_user_id == user.user_id,
            ))
        if row is None:
            raise HTTPException(status_code=404, detail="Jarvis run was not found.")
        return self.run_payload(row)
