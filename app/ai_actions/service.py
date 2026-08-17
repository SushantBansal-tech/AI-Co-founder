from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from app.ai_actions.state_machine import AIActionStatus, assert_transition
from app.authority.decisions import AuthorityDecisionResult, AuthorityOutcome
from app.database.models.ai_action import AIActionRequest, ApprovalDecision
from app.database.models.authority import AuthorityApprovalRequest
from app.database.models.customer import Customer
from app.database.models.lead import Lead
from app.database.models.structured import (
    GstRateRecord,
    InventoryRecord,
    ProductCostRecord,
    ProductPriceRecord,
)
from app.events.service import record_business_event
from app.idempotency.service import hash_request


def utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def iso(value) -> str | None:
    return value.isoformat() if value else None


def _money(value: Any) -> Decimal:
    return Decimal(str(value or 0))


class AIActionService:
    """Durable Batch 4 lifecycle around registered business-tool calls."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def payload(row: AIActionRequest) -> dict:
        return {
            "id": row.id,
            "business_id": row.business_id,
            "principal_id": row.principal_id,
            "tool_name": row.tool_name,
            "action_type": row.action_type,
            "risk_level": row.risk_level,
            "status": row.status,
            "reason": row.reason,
            "arguments": row.arguments_json,
            "idempotency_key": row.idempotency_key,
            "customer_id": row.customer_id,
            "lead_id": row.lead_id,
            "thread_id": row.thread_id,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "latest_authority_decision_id": row.latest_authority_decision_id,
            "active_approval_request_id": row.active_approval_request_id,
            "latest_tool_execution_id": row.latest_tool_execution_id,
            "policy_code": row.policy_code,
            "policy_version": row.policy_version,
            "settings_version": row.settings_version,
            "approval_role": row.approval_role,
            "evaluated_facts": row.evaluated_facts_json,
            "execution_result": row.execution_result_json,
            "error_code": row.error_code,
            "error_message": row.error_message,
            "execution_attempt_count": row.execution_attempt_count,
            "proposed_at": iso(row.proposed_at),
            "evaluated_at": iso(row.evaluated_at),
            "approved_at": iso(row.approved_at),
            "execution_started_at": iso(row.execution_started_at),
            "completed_at": iso(row.completed_at),
            "updated_at": iso(row.updated_at),
        }

    async def propose(
        self, *, business_id: str, principal_id: str, tool_name: str,
        action_type: str, risk_level: str, arguments: dict,
        idempotency_key: str | None, reason: str | None = None,
    ) -> AIActionRequest:
        async with self.session_factory() as session:
            existing = None
            if idempotency_key:
                existing = await session.scalar(select(AIActionRequest).where(
                    AIActionRequest.business_id == business_id,
                    AIActionRequest.principal_id == principal_id,
                    AIActionRequest.tool_name == tool_name,
                    AIActionRequest.idempotency_key == idempotency_key,
                ).with_for_update())
            if existing is not None:
                if existing.input_hash != hash_request(arguments):
                    raise HTTPException(
                        status_code=409,
                        detail="The action idempotency key was already used with different arguments.",
                    )
                return existing

            lead_id = arguments.get("lead_id")
            customer_id = arguments.get("customer_id")
            thread_id = arguments.get("thread_id")
            if lead_id and (customer_id is None or thread_id is None):
                lead = await session.scalar(select(Lead).where(
                    Lead.id == lead_id, Lead.business_id == business_id,
                ))
                if lead:
                    customer_id = customer_id or lead.customer_id
                    thread_id = thread_id or lead.thread_id
            row = AIActionRequest(
                id=str(uuid4()), business_id=business_id,
                principal_id=principal_id, tool_name=tool_name,
                action_type=action_type, risk_level=risk_level,
                reason=reason, arguments_json=arguments,
                input_hash=hash_request(arguments),
                idempotency_key=idempotency_key,
                customer_id=customer_id, lead_id=lead_id, thread_id=thread_id,
            )
            session.add(row)
            await record_business_event(
                session, business_id=business_id, thread_id=thread_id,
                event_type="ai.action_proposed", source="jarvis",
                actor_type="ai_principal", actor_id=principal_id,
                entity_type="ai_action_request", entity_id=row.id,
                data={"tool_name": tool_name, "action_type": action_type,
                      "risk_level": risk_level, "reason": reason},
            )
            await session.commit()
            return row

    async def build_authoritative_facts(
        self, *, action: AIActionRequest, arguments: dict,
    ) -> dict:
        """Add current DB facts so an old approval cannot authorize changed data."""
        facts = dict(arguments)
        facts.update({
            "action_request_id": action.id,
            "entity_type": action.entity_type,
            "entity_id": action.entity_id,
            "thread_id": action.thread_id,
            "customer_id": action.customer_id,
        })
        missing_master_data: list[str] = list(facts.get("missing_master_data") or [])
        snapshot: dict[str, Any] = {}
        product_code = arguments.get("product_code")

        async with self.session_factory() as session:
            if product_code:
                price = await session.scalar(select(ProductPriceRecord).where(
                    ProductPriceRecord.business_id == action.business_id,
                    ProductPriceRecord.product_code == product_code,
                    ProductPriceRecord.is_active.is_(True),
                    ProductPriceRecord.status == "active",
                ).order_by(ProductPriceRecord.created_at.desc()))
                cost = await session.scalar(select(ProductCostRecord).where(
                    ProductCostRecord.business_id == action.business_id,
                    ProductCostRecord.product_code == product_code,
                    ProductCostRecord.is_active.is_(True),
                ).order_by(ProductCostRecord.created_at.desc()))
                gst = await session.scalar(select(GstRateRecord).where(
                    GstRateRecord.business_id == action.business_id,
                    GstRateRecord.product_code == product_code,
                    GstRateRecord.is_active.is_(True),
                    GstRateRecord.status == "active",
                ).order_by(GstRateRecord.created_at.desc()))
                available = await session.scalar(select(
                    func.coalesce(func.sum(InventoryRecord.available_qty), 0)
                ).where(
                    InventoryRecord.business_id == action.business_id,
                    InventoryRecord.product_code == product_code,
                    InventoryRecord.is_active.is_(True),
                ))
                snapshot.update({
                    "product_code": product_code,
                    "price_record_id": price.id if price else None,
                    "base_price_inr": str(price.base_price_inr) if price else None,
                    "cost_record_id": cost.id if cost else None,
                    "rm_cost_per_mt": str(cost.rm_cost_per_mt) if cost else None,
                    "gst_record_id": gst.id if gst else None,
                    "gst_rate_pct": str(gst.gst_rate_pct) if gst else None,
                    "available_quantity": str(available or 0),
                })
                if not price:
                    missing_master_data.append(f"product_price:{product_code}")
                if not cost:
                    missing_master_data.append(f"product_cost:{product_code}")
                if not gst:
                    missing_master_data.append(f"gst_rate:{product_code}")
                quantity = _money(arguments.get("quantity"))
                discount = _money(arguments.get("requested_discount_pct"))
                if price:
                    net_price = _money(price.base_price_inr) * (Decimal("1") - discount / 100)
                    facts.setdefault("quotation_value", net_price * quantity)
                    facts.setdefault("discount_pct", discount)
                    if cost and net_price:
                        total_cost = _money(cost.rm_cost_per_mt) * (
                            Decimal("1") + _money(cost.manufacturing_overhead_pct) / 100
                        )
                        facts.setdefault(
                            "resulting_margin_pct",
                            ((net_price - total_cost) / net_price) * 100,
                        )
            if action.customer_id:
                customer = await session.scalar(select(Customer).where(
                    Customer.id == action.customer_id,
                    Customer.business_id == action.business_id,
                ))
                if customer:
                    snapshot["customer_credit_limit"] = str(customer.credit_limit or 0)
                    snapshot["customer_outstanding_amount"] = str(
                        customer.outstanding_amount or 0
                    )
                    snapshot["customer_payment_behavior"] = str(customer.payment_behavior)

        facts["missing_master_data"] = sorted(set(missing_master_data))
        facts["authoritative_snapshot"] = snapshot
        return facts

    async def record_evaluation(
        self, action_id: str, result: AuthorityDecisionResult, *, revalidating: bool = False,
    ) -> AIActionRequest:
        status_map = {
            AuthorityOutcome.ALLOW: AIActionStatus.ALLOWED,
            AuthorityOutcome.REQUIRE_APPROVAL: AIActionStatus.AWAITING_APPROVAL,
            AuthorityOutcome.DENY: AIActionStatus.DENIED,
            AuthorityOutcome.REQUIRE_MORE_INFORMATION: AIActionStatus.BLOCKED,
            AuthorityOutcome.BLOCKED_MASTER_DATA: AIActionStatus.BLOCKED,
        }
        target = status_map[result.decision]
        async with self.session_factory() as session:
            row = await session.scalar(select(AIActionRequest).where(
                AIActionRequest.id == action_id,
            ).with_for_update())
            if row is None:
                raise HTTPException(status_code=404, detail="AI action was not found.")
            if not revalidating:
                if row.status == AIActionStatus.PROPOSED:
                    assert_transition(row.status, AIActionStatus.EVALUATED)
                    row.status = AIActionStatus.EVALUATED
                assert_transition(row.status, target)
            else:
                assert_transition(row.status, target if target != AIActionStatus.ALLOWED
                                  else AIActionStatus.EXECUTING)
            row.status = (
                AIActionStatus.EXECUTING
                if revalidating and target == AIActionStatus.ALLOWED else target
            )
            row.latest_authority_decision_id = result.decision_id
            row.active_approval_request_id = result.approval_request_id
            row.policy_code = result.policy_code
            row.policy_version = result.policy_version
            row.settings_version = result.settings_version
            row.approval_role = result.approval_role
            row.evaluated_facts_json = result.evaluated_facts
            row.evaluated_at = utc_now()
            if row.status == AIActionStatus.EXECUTING:
                row.execution_started_at = utc_now()
                row.execution_attempt_count += 1
            await session.commit()
            return row

    async def begin_execution(self, action_id: str, execution_id: str) -> AIActionRequest:
        async with self.session_factory() as session:
            row = await session.scalar(select(AIActionRequest).where(
                AIActionRequest.id == action_id,
            ).with_for_update())
            if row is None:
                raise HTTPException(status_code=404, detail="AI action was not found.")
            assert_transition(row.status, AIActionStatus.EXECUTING)
            row.status = AIActionStatus.EXECUTING
            row.latest_tool_execution_id = execution_id
            row.execution_started_at = utc_now()
            row.execution_attempt_count += 1
            await session.commit()
            return row

    async def allow_without_policy(self, action_id: str) -> AIActionRequest:
        """Read-only registered tools are authorized by scope, not business policy."""
        async with self.session_factory() as session:
            row = await session.scalar(select(AIActionRequest).where(
                AIActionRequest.id == action_id,
            ).with_for_update())
            if row is None:
                raise HTTPException(status_code=404, detail="AI action was not found.")
            assert_transition(row.status, AIActionStatus.ALLOWED)
            row.status = AIActionStatus.ALLOWED
            row.evaluated_at = utc_now()
            await session.commit()
            return row

    async def begin_revalidation(
        self, *, action_id: str, business_id: str, principal_id: str,
        execution_id: str,
    ) -> AIActionRequest:
        async with self.session_factory() as session:
            row = await session.scalar(select(AIActionRequest).where(
                AIActionRequest.id == action_id,
                AIActionRequest.business_id == business_id,
                AIActionRequest.principal_id == principal_id,
            ).with_for_update())
            if row is None:
                raise HTTPException(status_code=404, detail="AI action was not found.")
            if row.status != AIActionStatus.APPROVED:
                raise HTTPException(
                    status_code=409,
                    detail=f"Only an APPROVED action can resume; current status is {row.status}.",
                )
            assert_transition(row.status, AIActionStatus.REVALIDATING)
            row.status = AIActionStatus.REVALIDATING
            row.latest_tool_execution_id = execution_id
            await session.commit()
            return row

    async def finish(
        self, action_id: str, *, succeeded: bool, result: dict | None = None,
        error_code: str | None = None, error_message: str | None = None,
        entity_type: str | None = None, entity_id: str | None = None,
    ) -> None:
        target = AIActionStatus.SUCCEEDED if succeeded else AIActionStatus.FAILED
        async with self.session_factory() as session:
            row = await session.scalar(select(AIActionRequest).where(
                AIActionRequest.id == action_id,
            ).with_for_update())
            if row is None:
                return
            assert_transition(row.status, target)
            row.status = target
            row.execution_result_json = result
            row.error_code = error_code
            row.error_message = error_message
            row.entity_type = entity_type or row.entity_type
            row.entity_id = entity_id or row.entity_id
            row.completed_at = utc_now()
            await record_business_event(
                session, business_id=row.business_id, thread_id=row.thread_id,
                event_type="ai.action_succeeded" if succeeded else "ai.action_failed",
                source="jarvis", actor_type="ai_principal", actor_id=row.principal_id,
                entity_type="ai_action_request", entity_id=row.id,
                data={"tool_name": row.tool_name, "entity_type": row.entity_type,
                      "entity_id": row.entity_id, "error_code": error_code},
            )
            await session.commit()

    async def get_for_principal(
        self, *, action_id: str, business_id: str, principal_id: str,
    ) -> AIActionRequest:
        async with self.session_factory() as session:
            row = await session.scalar(select(AIActionRequest).where(
                AIActionRequest.id == action_id,
                AIActionRequest.business_id == business_id,
                AIActionRequest.principal_id == principal_id,
            ))
        if row is None:
            raise HTTPException(status_code=404, detail="AI action was not found.")
        return row

    async def list_for_user(self, user, *, status: str | None, limit: int) -> list[dict]:
        async with self.session_factory() as session:
            query = select(AIActionRequest).where(
                AIActionRequest.business_id == user.business_id
            )
            if status:
                query = query.where(AIActionRequest.status == status.upper())
            rows = (await session.scalars(
                query.order_by(AIActionRequest.proposed_at.desc()).limit(limit)
            )).all()
        return [self.payload(row) for row in rows]

    async def get_for_user(self, user, action_id: str) -> dict:
        async with self.session_factory() as session:
            row = await session.scalar(select(AIActionRequest).where(
                AIActionRequest.id == action_id,
                AIActionRequest.business_id == user.business_id,
            ))
            if row is None:
                raise HTTPException(status_code=404, detail="AI action was not found.")
            decisions = (await session.scalars(select(ApprovalDecision).where(
                ApprovalDecision.action_request_id == row.id,
            ).order_by(ApprovalDecision.decided_at))).all()
        payload = self.payload(row)
        payload["approval_decisions"] = [{
            "id": item.id, "approval_request_id": item.approval_request_id,
            "decision": item.decision, "decided_by_user_id": item.decided_by_user_id,
            "decided_by_role": item.decided_by_role, "reason": item.reason,
            "policy_version": item.policy_version,
            "settings_version": item.settings_version,
            "decided_at": iso(item.decided_at),
        } for item in decisions]
        return payload
