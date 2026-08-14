import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select

from app.business_tools.context import ToolContext
from app.business_tools.schemas import AddCustomerNoteInput, CreateTaskInput, CustomerIdInput
from app.customers.customer_360 import get_customer_360
from app.database.models.crm import BusinessMembership, CRMActivity, CRMTask
from app.database.models.customer import Customer
from app.database.models.followup_job import FollowUpJob
from app.database.models.lead import Lead
from app.database.models.memory import CustomerNote, MemoryOutbox
from app.database.models.pipeline import PipelineInstance
from app.database.models.quotation import QuotationRecord, QuotationStatus, QuotationVersion
from app.database.models.structured import (
    DiscountBandRecord,
    GstRateRecord,
    InventoryRecord,
    MarginRuleRecord,
    ProductCostRecord,
    ProductPriceRecord,
    TransportRateRecord,
)
from app.events.service import record_business_event
from app.followups.service import schedule_quotation_followups


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _enum(value):
    return value.value if hasattr(value, "value") else value


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _decimal(value) -> Decimal:
    return Decimal(str(value or 0))


class BusinessToolHandlers:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    async def search_customers(self, context: ToolContext, args) -> dict:
        conditions = [
            Customer.business_id == context.business_id,
            Customer.status != "merged",
        ]
        if args.search:
            term = f"%{args.search.strip().lower()}%"
            conditions.append(or_(
                func.lower(Customer.company_name).like(term),
                func.lower(Customer.contact_person).like(term),
                func.lower(Customer.email).like(term),
                func.lower(Customer.phone).like(term),
                func.lower(Customer.gstin).like(term),
            ))
        async with self.session_factory() as session:
            rows = (await session.scalars(
                select(Customer).where(*conditions)
                .order_by(Customer.updated_at.desc()).limit(args.limit)
            )).all()
        items = [{
            "id": row.id,
            "company_name": row.company_name,
            "contact_person": row.contact_person,
            "email": row.email,
            "phone": row.phone,
            "gstin": row.gstin,
            "city": row.city,
            "status": row.status,
            "account_owner_id": row.account_owner_id,
        } for row in rows]
        return {"items": items, "count": len(items)}

    async def get_customer_360(self, context: ToolContext, args: CustomerIdInput) -> dict:
        async with self.session_factory() as session:
            try:
                return await get_customer_360(
                    session, business_id=context.business_id,
                    customer_id=args.customer_id,
                )
            except ValueError as exc:
                raise HTTPException(status_code=404, detail="Customer was not found.") from exc

    async def get_lead(self, context: ToolContext, args) -> dict:
        async with self.session_factory() as session:
            lead = await session.scalar(select(Lead).where(
                Lead.id == args.lead_id,
                Lead.business_id == context.business_id,
            ))
            if lead is None:
                raise HTTPException(status_code=404, detail="Lead was not found.")
            return {
                "id": lead.id, "business_id": lead.business_id,
                "customer_id": lead.customer_id, "thread_id": lead.thread_id,
                "inquiry_id": lead.inquiry_id, "source": _enum(lead.source),
                "company_name": lead.company_name, "contact_person": lead.contact_person,
                "sender_identifier": lead.sender_identifier,
                "product_requested": lead.product_requested, "quantity": lead.quantity,
                "specifications": lead.specifications,
                "delivery_location": lead.delivery_location,
                "delivery_date": lead.delivery_date, "status": _enum(lead.status),
                "assigned_to_user_id": lead.assigned_to_user_id,
                "created_at": _iso(lead.created_at), "updated_at": _iso(lead.updated_at),
            }

    async def get_pipeline(self, context: ToolContext, args) -> dict:
        conditions = [PipelineInstance.business_id == context.business_id]
        conditions.append(
            PipelineInstance.thread_id == args.thread_id
            if args.thread_id else PipelineInstance.lead_id == args.lead_id
        )
        async with self.session_factory() as session:
            row = await session.scalar(select(PipelineInstance).where(*conditions))
            if row is None:
                raise HTTPException(status_code=404, detail="Pipeline was not found.")
            return {
                "id": row.id, "thread_id": row.thread_id,
                "customer_id": row.customer_id, "lead_id": row.lead_id,
                "pipeline_status": row.pipeline_status,
                "business_milestone": row.business_milestone,
                "waiting_for": row.waiting_for, "approval_stage": row.approval_stage,
                "status_reason": row.status_reason, "current_node": row.current_node,
                "failure_category": row.failure_category,
                "failure_code": row.failure_code, "updated_at": _iso(row.updated_at),
            }

    async def get_pending_approvals(self, context: ToolContext, args) -> dict:
        async with self.session_factory() as session:
            rows = (await session.execute(
                select(PipelineInstance, Lead, Customer)
                .join(Lead, and_(
                    Lead.id == PipelineInstance.lead_id,
                    Lead.business_id == PipelineInstance.business_id,
                ))
                .outerjoin(Customer, and_(
                    Customer.id == PipelineInstance.customer_id,
                    Customer.business_id == PipelineInstance.business_id,
                ))
                .where(
                    PipelineInstance.business_id == context.business_id,
                    PipelineInstance.pipeline_status == "awaiting_approval",
                )
                .order_by(PipelineInstance.updated_at).limit(args.limit)
            )).all()
        items = [{
            "thread_id": pipeline.thread_id, "lead_id": lead.id,
            "customer_id": pipeline.customer_id,
            "customer_name": customer.company_name if customer else lead.company_name,
            "approval_stage": pipeline.approval_stage,
            "waiting_for": pipeline.waiting_for,
            "reason": pipeline.status_reason,
            "updated_at": _iso(pipeline.updated_at),
        } for pipeline, lead, customer in rows]
        return {"items": items, "count": len(items)}

    async def get_inventory(self, context: ToolContext, args) -> dict:
        conditions = [
            InventoryRecord.business_id == context.business_id,
            InventoryRecord.is_active.is_(True),
        ]
        if args.product_code:
            conditions.append(InventoryRecord.product_code == args.product_code)
        if args.warehouse:
            conditions.append(func.lower(InventoryRecord.warehouse) == args.warehouse.lower())
        async with self.session_factory() as session:
            rows = (await session.scalars(
                select(InventoryRecord).where(*conditions)
                .order_by(InventoryRecord.product_code, InventoryRecord.warehouse)
                .limit(args.limit)
            )).all()
        items = [{
            "id": row.id, "product_code": row.product_code,
            "product_name": row.product_name, "warehouse": row.warehouse,
            "physical_qty": _decimal(row.physical_qty),
            "reserved_qty": _decimal(row.reserved_qty),
            "available_qty": _decimal(row.available_qty),
            "damaged_qty": _decimal(row.damaged_qty),
            "stock_status": row.stock_status,
            "last_updated": _iso(row.last_updated),
        } for row in rows]
        return {
            "items": items,
            "total_available_quantity": sum(
                (_decimal(row.available_qty) for row in rows), Decimal("0")
            ),
            "count": len(items),
        }

    async def get_pricing_inputs(self, context: ToolContext, args) -> dict:
        async with self.session_factory() as session:
            async def rows(model, *extra):
                return (await session.scalars(select(model).where(
                    model.business_id == context.business_id,
                    model.is_active.is_(True), *extra,
                ))).all()

            prices = await rows(ProductPriceRecord, ProductPriceRecord.product_code == args.product_code)
            costs = await rows(ProductCostRecord, ProductCostRecord.product_code == args.product_code)
            margins = await rows(MarginRuleRecord, or_(
                MarginRuleRecord.product_code == args.product_code,
                MarginRuleRecord.product_code.is_(None),
            ))
            gst = await rows(GstRateRecord, or_(
                GstRateRecord.product_code == args.product_code,
                GstRateRecord.product_code.is_(None),
            ))
            transport_extra = []
            if args.destination_city:
                transport_extra.append(
                    func.lower(TransportRateRecord.destination_city) == args.destination_city.lower()
                )
            transport = await rows(TransportRateRecord, *transport_extra)
            discount_extra = []
            if args.customer_type:
                discount_extra.append(
                    func.lower(DiscountBandRecord.customer_type) == args.customer_type.lower()
                )
            if args.order_value is not None:
                discount_extra.extend([
                    DiscountBandRecord.order_value_min <= args.order_value,
                    DiscountBandRecord.order_value_max >= args.order_value,
                ])
            discounts = await rows(DiscountBandRecord, *discount_extra)

        def selected(row, names):
            return {name: (
                _decimal(getattr(row, name))
                if isinstance(getattr(row, name), Decimal)
                else _iso(getattr(row, name))
                if isinstance(getattr(row, name), (date, datetime))
                else getattr(row, name)
            ) for name in names}

        return {
            "product_code": args.product_code,
            "prices": [selected(r, ("product_code", "unit", "base_price_inr", "currency", "effective_from", "effective_to", "minimum_order_qty", "status")) for r in prices],
            "costs": [selected(r, ("product_code", "rm_cost_per_mt", "manufacturing_overhead_pct")) for r in costs],
            "transport": [selected(r, ("destination_city", "zone", "rate_per_mt_inr", "minimum_charge_inr", "handling_charge_inr", "estimated_transit_days", "status")) for r in transport],
            "discounts": [selected(r, ("customer_type", "order_value_min", "order_value_max", "max_discount_pct", "approval_limit_pct")) for r in discounts],
            "margins": [selected(r, ("product_code", "product_category", "minimum_margin_pct", "target_margin_pct", "exception_approver", "status")) for r in margins],
            "gst": [selected(r, ("product_code", "product_category", "hsn_code", "gst_rate_pct", "effective_from", "status")) for r in gst],
        }

    async def get_open_tasks(self, context: ToolContext, args) -> dict:
        conditions = [
            CRMTask.business_id == context.business_id,
            CRMTask.status.in_(("open", "in_progress")),
        ]
        if args.customer_id:
            conditions.append(CRMTask.customer_id == args.customer_id)
        if args.lead_id:
            conditions.append(CRMTask.lead_id == args.lead_id)
        if args.assigned_to_user_id:
            conditions.append(CRMTask.assigned_to_user_id == args.assigned_to_user_id)
        if args.overdue_only:
            conditions.append(CRMTask.due_at < datetime.now(UTC).replace(tzinfo=None))
        async with self.session_factory() as session:
            rows = (await session.scalars(
                select(CRMTask).where(*conditions).order_by(CRMTask.due_at).limit(args.limit)
            )).all()
        items = [{
            "id": row.id, "customer_id": row.customer_id, "lead_id": row.lead_id,
            "thread_id": row.thread_id, "assigned_to_user_id": row.assigned_to_user_id,
            "task_type": row.task_type, "title": row.title,
            "description": row.description, "priority": row.priority,
            "status": row.status, "due_at": _iso(row.due_at),
            "created_by_user_id": row.created_by_user_id,
            "created_by_principal_id": row.created_by_principal_id,
        } for row in rows]
        return {"items": items, "count": len(items)}

    async def _validate_links(self, session, context, customer_id=None, lead_id=None):
        if customer_id and not await session.scalar(select(Customer.id).where(
            Customer.id == customer_id, Customer.business_id == context.business_id,
        )):
            raise HTTPException(status_code=404, detail="Customer was not found.")
        if lead_id and not await session.scalar(select(Lead.id).where(
            Lead.id == lead_id, Lead.business_id == context.business_id,
        )):
            raise HTTPException(status_code=404, detail="Lead was not found.")

    async def add_customer_note(self, context: ToolContext, args: AddCustomerNoteInput) -> dict:
        async with self.session_factory() as session:
            await self._validate_links(session, context, customer_id=args.customer_id)
            note = CustomerNote(
                business_id=context.business_id, customer_id=args.customer_id,
                thread_id=args.thread_id, request_event_id=context.execution_id,
                content_type=args.content_type, content=args.content,
                created_by=f"ai:{context.principal_id}",
                created_by_principal_id=context.principal_id,
            )
            session.add(note)
            await session.flush()
            session.add(MemoryOutbox(
                business_id=context.business_id, customer_id=args.customer_id,
                source_type="customer_note", source_id=note.id,
                memory_type=args.content_type, content=args.content,
                thread_id=args.thread_id,
            ))
            await record_business_event(
                session, business_id=context.business_id,
                customer_id=args.customer_id, thread_id=args.thread_id,
                event_type="ai_customer_note_added", source="business_tool",
                actor_type="ai_principal", actor_id=context.principal_id,
                entity_type="customer_note", entity_id=note.id,
                data={"content_type": args.content_type, "tool_execution_id": context.execution_id},
            )
            await session.commit()
            return {
                "id": note.id, "customer_id": note.customer_id,
                "content_type": note.content_type, "content": note.content,
                "status": note.status, "occurred_at": _iso(note.occurred_at),
            }

    async def create_task(self, context: ToolContext, args: CreateTaskInput) -> dict:
        async with self.session_factory() as session:
            await self._validate_links(
                session, context, customer_id=args.customer_id, lead_id=args.lead_id
            )
            membership = await session.scalar(select(BusinessMembership.id).where(
                BusinessMembership.business_id == context.business_id,
                BusinessMembership.user_id == args.assigned_to_user_id,
                BusinessMembership.status == "active",
            ))
            if membership is None:
                raise HTTPException(status_code=422, detail="Assignee is not an active business member.")
            task = CRMTask(
                business_id=context.business_id,
                created_by_user_id=None,
                created_by_principal_id=context.principal_id,
                **args.model_dump(),
            )
            session.add(task)
            await session.flush()
            await record_business_event(
                session, business_id=context.business_id,
                customer_id=task.customer_id, lead_id=task.lead_id,
                thread_id=task.thread_id, event_type="ai_crm_task_created",
                source="business_tool", actor_type="ai_principal",
                actor_id=context.principal_id, entity_type="crm_task",
                entity_id=task.id, data={
                    "assigned_to_user_id": task.assigned_to_user_id,
                    "tool_execution_id": context.execution_id,
                },
            )
            await session.commit()
            return {
                "id": task.id, "customer_id": task.customer_id,
                "lead_id": task.lead_id,
                "assigned_to_user_id": task.assigned_to_user_id,
                "created_by_principal_id": task.created_by_principal_id,
                "title": task.title, "status": task.status,
                "due_at": _iso(task.due_at),
            }

    async def record_activity(self, context: ToolContext, args) -> dict:
        async with self.session_factory() as session:
            await self._validate_links(
                session, context, customer_id=args.customer_id, lead_id=args.lead_id
            )
            if args.task_id and not await session.scalar(select(CRMTask.id).where(
                CRMTask.id == args.task_id,
                CRMTask.business_id == context.business_id,
            )):
                raise HTTPException(status_code=404, detail="Task was not found.")
            values = args.model_dump()
            values["occurred_at"] = values["occurred_at"] or datetime.now(UTC).replace(tzinfo=None)
            activity = CRMActivity(
                business_id=context.business_id, actor_user_id=None,
                actor_principal_id=context.principal_id, **values,
            )
            session.add(activity)
            await session.flush()
            await record_business_event(
                session, business_id=context.business_id,
                customer_id=activity.customer_id, lead_id=activity.lead_id,
                thread_id=activity.thread_id,
                event_type="ai_crm_activity_recorded", source="business_tool",
                actor_type="ai_principal", actor_id=context.principal_id,
                entity_type="crm_activity", entity_id=activity.id,
                data={"activity_type": activity.activity_type,
                      "tool_execution_id": context.execution_id},
            )
            await session.commit()
            return {
                "id": activity.id, "activity_type": activity.activity_type,
                "subject": activity.subject, "outcome": activity.outcome,
                "actor_principal_id": activity.actor_principal_id,
                "occurred_at": _iso(activity.occurred_at),
            }

    async def schedule_followup(self, context: ToolContext, args) -> dict:
        async with self.session_factory() as session:
            quotation = await session.scalar(select(QuotationRecord).where(
                QuotationRecord.id == args.quotation_id,
                QuotationRecord.business_id == context.business_id,
            ))
            if quotation is None:
                raise HTTPException(status_code=404, detail="Quotation was not found.")
            if _enum(quotation.status) != QuotationStatus.SENT.value or not quotation.sent_at:
                raise HTTPException(status_code=409, detail="Follow-ups require a sent quotation.")
            jobs_created = await schedule_quotation_followups(
                session, business_id=context.business_id,
                customer_id=quotation.customer_id, lead_id=None,
                thread_id=quotation.thread_id, quotation_id=quotation.id,
                quotation_number=quotation.quotation_number,
                sent_at=quotation.sent_at, channel=quotation.sent_via or "",
                recipient=quotation.sent_to or "", max_attempts=args.max_attempts,
                created_by_principal_id=context.principal_id,
            )
            await record_business_event(
                session, business_id=context.business_id,
                customer_id=quotation.customer_id, thread_id=quotation.thread_id,
                event_type="ai_followup_scheduled", source="business_tool",
                actor_type="ai_principal", actor_id=context.principal_id,
                entity_type="quotation", entity_id=quotation.id,
                data={"jobs_created": jobs_created,
                      "tool_execution_id": context.execution_id},
            )
            await session.commit()
            return {
                "quotation_id": quotation.id,
                "quotation_number": quotation.quotation_number,
                "jobs_created": jobs_created, "status": "scheduled",
            }

    async def prepare_quotation(self, context: ToolContext, args) -> dict:
        today = date.today()
        async with self.session_factory() as session:
            lead = await session.scalar(select(Lead).where(
                Lead.id == args.lead_id, Lead.business_id == context.business_id,
            ))
            if lead is None:
                raise HTTPException(status_code=404, detail="Lead was not found.")
            price = await session.scalar(select(ProductPriceRecord).where(
                ProductPriceRecord.business_id == context.business_id,
                ProductPriceRecord.product_code == args.product_code,
                ProductPriceRecord.is_active.is_(True),
                or_(ProductPriceRecord.effective_from.is_(None), ProductPriceRecord.effective_from <= today),
                or_(ProductPriceRecord.effective_to.is_(None), ProductPriceRecord.effective_to >= today),
            ).order_by(ProductPriceRecord.created_at.desc()))
            cost = await session.scalar(select(ProductCostRecord).where(
                ProductCostRecord.business_id == context.business_id,
                ProductCostRecord.product_code == args.product_code,
                ProductCostRecord.is_active.is_(True),
            ).order_by(ProductCostRecord.created_at.desc()))
            gst = await session.scalar(select(GstRateRecord).where(
                GstRateRecord.business_id == context.business_id,
                GstRateRecord.is_active.is_(True),
                or_(GstRateRecord.product_code == args.product_code, GstRateRecord.product_code.is_(None)),
            ).order_by(GstRateRecord.product_code.desc(), GstRateRecord.created_at.desc()))
            if not price or not cost or not gst:
                missing = [name for name, row in (("price", price), ("cost", cost), ("gst", gst)) if row is None]
                raise HTTPException(status_code=409, detail={
                    "message": "Required pricing master data is missing.",
                    "missing": missing,
                })
            base_price = _decimal(price.base_price_inr)
            unit_price = _money(base_price * (Decimal("1") - args.requested_discount_pct / Decimal("100")))
            total_cost = _decimal(cost.rm_cost_per_mt) * (
                Decimal("1") + _decimal(cost.manufacturing_overhead_pct) / Decimal("100")
            )
            margin = _money(((unit_price - total_cost) / unit_price) * Decimal("100")) if unit_price else Decimal("-100")
            subtotal = _money(unit_price * args.quantity)
            gst_rate = _decimal(gst.gst_rate_pct)
            gst_amount = _money(subtotal * gst_rate / Decimal("100"))
            total = _money(subtotal + gst_amount)
            valid_until = today + timedelta(days=args.validity_days)
            number = f"QT-D-{today.year}-{uuid4().hex[:8].upper()}"
            draft = {
                "quotation_number": number, "lead_id": lead.id,
                "inquiry_id": lead.inquiry_id, "customer_id": lead.customer_id,
                "buyer_company": lead.company_name or "Customer",
                "product_code": args.product_code, "quantity": str(args.quantity),
                "unit_price_ex_gst": str(unit_price),
                "discount_pct": str(args.requested_discount_pct),
                "resulting_margin_pct": str(margin), "subtotal_ex_gst": str(subtotal),
                "gst_rate_pct": str(gst_rate), "gst_amount": str(gst_amount),
                "total_inc_gst": str(total), "valid_until": valid_until.isoformat(),
                "status": "draft", "dispatched": False,
            }
            quotation = QuotationRecord(
                business_id=context.business_id, customer_id=lead.customer_id,
                thread_id=lead.thread_id, quotation_number=number,
                inquiry_id=lead.inquiry_id, status=QuotationStatus.DRAFT,
                buyer_company=lead.company_name or "Customer",
                total_inc_gst=float(total), requires_approval=True,
                draft_json=json.dumps(draft), html_content="",
                prepared_by_principal_id=context.principal_id,
            )
            session.add(quotation)
            await session.flush()
            session.add(QuotationVersion(
                business_id=context.business_id, customer_id=lead.customer_id,
                thread_id=lead.thread_id, quotation_id=quotation.id,
                quotation_number=number, version_number=1,
                price_per_mt_ex_gst=float(unit_price),
                discount_pct=float(args.requested_discount_pct),
                subtotal_ex_gst=float(subtotal), gst_amount=float(gst_amount),
                total_inc_gst=float(total),
                change_reason="Prepared by controlled Jarvis tool",
                changed_by=f"ai:{context.principal_id}",
                changed_by_principal_id=context.principal_id,
                draft_json=json.dumps(draft), html_content="",
            ))
            await record_business_event(
                session, business_id=context.business_id,
                customer_id=lead.customer_id, lead_id=lead.id,
                thread_id=lead.thread_id,
                event_type="ai_quotation_draft_prepared", source="business_tool",
                actor_type="ai_principal", actor_id=context.principal_id,
                entity_type="quotation", entity_id=quotation.id,
                data={"quotation_number": number, "total_inc_gst": float(total),
                      "tool_execution_id": context.execution_id,
                      "dispatched": False},
            )
            await session.commit()
            return {
                "quotation_id": quotation.id, "quotation_number": number,
                "status": "draft", "product_code": args.product_code,
                "quantity": args.quantity, "unit_price_ex_gst": unit_price,
                "discount_pct": args.requested_discount_pct,
                "resulting_margin_pct": margin, "subtotal_ex_gst": subtotal,
                "gst_rate_pct": gst_rate, "gst_amount": gst_amount,
                "total_inc_gst": total, "valid_until": valid_until.isoformat(),
                "requires_approval": True, "dispatched": False,
            }
