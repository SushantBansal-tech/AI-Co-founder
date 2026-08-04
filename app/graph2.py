"""
complete_pipeline.py  —  Full Sales OS pipeline in one compiled LangGraph.

Every sub-agent from inquiry capture to department handoff is a node here.
The graph is re-invoked for the same lead multiple times using LangGraph
checkpointing (thread_id = inquiry_id).  A `trigger` field in the state
tells the entry router which sub-pipeline to activate.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TRIGGER VALUES and the path they activate:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  "inquiry"        → extract_inquiry → match_requirement → qualify_customer
                     → check_feasibility → compute_pricing → generate_quotation
                     → END  (status: QUOTATION_SENT)

  "followup"       → compose_reminder → dispatch_message
                     → END  (status: FOLLOWUP_SENT)

  "customer_reply" → analyze_reply
       ├─ counter_offer  → evaluate_counteroffer
       │        ├─ acceptable      → prepare_revised_quotation → dispatch_revised → END
       │        ├─ needs_approval  → request_approval → END
       │        └─ below_floor     → compose_rejection → dispatch_message → END
       └─ objection      → compose_objection_response → dispatch_message → END

  "po_received"    → extract_po_fields → validate_po
       ├─ valid / minor  → mark_order_won → create_sales_order
       │                   → build_handoff_packages → dispatch_handoff
       │                   → finalize_audit → END  (status: HANDED_OFF)
       └─ critical mismatch → send_correction_request → END
       └─ human_review      → request_approval → END

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HUMAN APPROVAL POINTS  (pipeline pauses):
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  credit_risk             → request_approval (stage="qualification")
  feasibility_review      → request_approval (stage="feasibility")
  large_order / discount  → request_approval (stage="pricing")
  below_floor_negotiation → request_approval (stage="negotiation")
  po_internal_mismatch    → request_approval (stage="po")

Resume by calling:
  POST /approve/{approval_request_id}  →  graph.ainvoke(state, config)
  with state["trigger"] = "approved" and state["approved_stage"] = stage

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os
import sys
import json
import asyncio
import operator
import re
from datetime import datetime, timezone
from typing import TypedDict, Annotated, Optional
from importlib import import_module
from pathlib import Path
from langgraph.graph import StateGraph, START, END
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import func, select
from app.rag.langgraph_adapter import LangGraphRAGAdapter
from app.rag.models import AgentRAGContext
from app.pipeline import (
    BusinessMilestone,
    PipelineStatus,
    WaitingFor,
    failure_result,
    persist_pipeline_snapshot,
)
from app.products import normalize_requirement as normalize_product_requirement
from app.products import verify_structured_product

#sys.path.insert(0, os.path.dirname(__file__))
APP_DIR = Path(__file__).resolve().parent
AGENTS_DIR = APP_DIR / "agents"

if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))


# ── Lazy module loader ────────────────────────────────────────────────────

def _mods() -> dict:
    return {
        "ia": import_module("01_Inquiry"),
        "cat": import_module("03_catalog"),
        "req": import_module("04_requirment"),
        "lk": import_module("05_customer_qual"),
        "qual": import_module("06_customer"),
        "inv": import_module("07_inventory"),
        "fe": import_module("08_feasiblity"),
        "pd": import_module("09_pricing"),
        "pe": import_module("10_pricing_agent"),
        "qb": import_module("11_quotation"),
        "qr": import_module("12_quotation"),
        "ft": import_module("13_followup"),
        "od": import_module("14_objection"),
        "fc": import_module("15_followup"),
        "ne": import_module("16_negotion"),
        "rv": import_module("17_revised"),
        "poe": import_module("18_PO"),
        "pov": import_module("19_PO"),
        "hb": import_module("20_handoff"),
        "hd": import_module("21_handoff"),
    }

# ═══════════════════════════════════════════════════════════════════════════
# COMPLETE PIPELINE STATE
# One TypedDict for the entire lead lifecycle.
# Checkpointed to PostgreSQL after every node so the graph can be
# re-invoked later (e.g. when PO arrives weeks after quotation).
# ═══════════════════════════════════════════════════════════════════════════

class CompletePipelineState(TypedDict):

    # ── Multi-tenant business scope ───────────────────────────────────
    business_id: str
    thread_id: str
    customer_id: Optional[str]
    customer_resolution: Optional[dict]
    customer_match_review_id: Optional[str]
    customer_360: Optional[dict]
    sales_context: Optional[dict]

    # ── Entry control ──────────────────────────────────────────────────
    trigger: str
    # Values:
    #   "inquiry"        – new customer message
    #   "followup"       – Celery beat calls this daily
    #   "customer_reply" – customer replied to quotation
    #   "po_received"    – customer sent Purchase Order
    #   "approved"       – human approved a paused step

    approved_stage: Optional[str]
    # Set when trigger=="approved" to know which stage to resume.
    # Values: "qualification" | "feasibility" | "pricing" | "negotiation" | "po"

    # ── Pipeline status ────────────────────────────────────────────────
    pipeline_status: Optional[str]
    business_milestone: Optional[str]
    waiting_for: str
    status_reason: Optional[str]
    current_node: Optional[str]
    status_updated_at: Optional[str]
    failure: Optional[dict]
    # Tracks where in the lifecycle the lead currently is:
    # "new" → "quotation_sent" → "followup_sent" → "negotiating"
    # → "po_received" → "won" → "handed_off"

    # ── Accumulating across all re-invocations ─────────────────────────
    stages_completed:       Annotated[list[str], operator.add]
    human_approval_reasons: Annotated[list[str], operator.add]
    rag_audit: Annotated[list[dict], operator.add]

    # ── Identifiers ────────────────────────────────────────────────────
    inquiry_id:       Optional[str]
    lead_id:          Optional[str]
    quotation_id:     Optional[str]
    quotation_number: Optional[str]
    sales_order_id:   Optional[str]
    po_id:            Optional[str]
    handoff_id:       Optional[str]

    # ── STAGE 1: Inquiry ───────────────────────────────────────────────
    source:            str
    raw_text:          str
    sender_identifier: Optional[str]
    extraction:        Optional[dict]   # InquiryExtraction.model_dump()
    needs_followup:    bool

    # ── STAGE 2: Requirement ───────────────────────────────────────────
    requirement: Optional[dict]         # RequirementSummary.model_dump()
    normalized_requirement: Optional[dict]
    product_candidate: Optional[dict]
    product_resolution_status: Optional[str]

    # ── STAGE 3: Qualification ─────────────────────────────────────────
    customer_profile: Optional[dict]    # CustomerProfile.model_dump()
    qualification:    Optional[dict]    # QualificationResult.model_dump()

    # ── STAGE 4: Feasibility ───────────────────────────────────────────
    feasibility: Optional[dict]         # FeasibilityResult.model_dump()

    # ── STAGE 5: Pricing ───────────────────────────────────────────────
    pricing: Optional[dict]             # PricingResult.model_dump()

    # ── STAGE 6: Quotation ─────────────────────────────────────────────
    final_draft_json:      Optional[str]  # QuotationDraft JSON (latest version)
    quotation_sent_at:     Optional[str]
    quotation_html:         Optional[str]
    quotation_message:      Optional[str]
    quotation_delivery_id:  Optional[str]

    # ── STAGE 7: Follow-up ─────────────────────────────────────────────
    followup_attempt:      int            # increments each reminder (1→4)
    followup_tone:         Optional[str]  # "gentle" | "moderate" | "urgent" | "final"
    followup_message:      Optional[str]  # composed message text
    followup_job_id:       Optional[str]
    followup_record_id:    Optional[str]
    followup_provider_message_id: Optional[str]

    # ── STAGE 8: Negotiation ───────────────────────────────────────────
    customer_reply_text:   Optional[str]  # latest customer message
    reply_type:            Optional[str]  # "counter_offer" | "objection" | "positive" | "po_text"
    objection:             Optional[dict] # ObjectionAnalysis.model_dump()
    negotiation_analysis:  Optional[dict] # NegotiationAnalysis.model_dump()
    revised_draft_json:    Optional[str]  # revised QuotationDraft JSON
    negotiation_version:   int            # increments each round

    # ── STAGE 9: PO Handling ───────────────────────────────────────────
    po_raw_text:     Optional[str]
    po_extraction:   Optional[dict]   # POExtraction.model_dump()
    po_validation:   Optional[dict]   # POValidationResult.model_dump()
    po_verdict:      Optional[str]    # "valid" | "minor_mismatch" | "critical_mismatch"
    order_revalidation: Optional[dict]
    order_won:       bool

    # ── STAGE 10: Handoff ──────────────────────────────────────────────
    handoff_summary_json:    Optional[str]
    departments_notified:    list[str]

    # ── Human approval (shared across stages) ─────────────────────────
    needs_human_approval: bool
    human_approval_stage: Optional[str]

    # ── Response to send to customer ──────────────────────────────────
    outbound_channel:   Optional[str]   # "email" | "whatsapp"
    outbound_recipient: Optional[str]
    outbound_message:   Optional[str]

    # ── Error ──────────────────────────────────────────────────────────
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _rebuild_requirement(req_dict: dict, req_mod, cat_mod):
    if not req_dict:
        return req_mod.RequirementSummary(
            inquiry_id="", match_type=req_mod.MatchType.CUSTOM,
            similarity_score=0.0, summary_text="",
        )
    d = {**req_dict}
    if d.get("matched_product"):
        d["matched_product"] = cat_mod.CatalogProduct(**d["matched_product"])
    if d.get("gap_analysis"):
        d["gap_analysis"] = req_mod.GapAnalysis(**d["gap_analysis"])
    return req_mod.RequirementSummary(**d)


def _mock_send(channel: str, recipient: str, message: str) -> bool:
    print(f"  [SEND {channel.upper()}] → {recipient}")
    print(f"  Preview: {message[:120].replace(chr(10),' ')}...")
    return True


# ═══════════════════════════════════════════════════════════════════════════
# COMMON RAG WRAPPER
# Retrieves agent-specific evidence before the business node runs.
# ═══════════════════════════════════════════════════════════════════════════

def with_rag_context(
    *,
    agent_name: str,
    rag_adapter: LangGraphRAGAdapter,
    handler,
    top_k: int = 5,
):
    async def wrapped(state: CompletePipelineState) -> dict:
        try:
            rag_context = await rag_adapter.get_context(
                agent_name=agent_name,
                state=state,
                top_k=top_k,
            )
        except Exception as exc:
            return {
                "error": f"{agent_name} retrieval failed: {exc}",
                "stages_completed": [],
                "rag_audit": [{
                    "agent_name": agent_name,
                    "query": None,
                    "chunk_ids": [],
                    "error": str(exc),
                }],
            }

        result = await handler(state, rag_context)

        audit_record = {
            "agent_name": agent_name,
            "query": rag_context.query,
            "chunk_ids": rag_context.chunk_ids,
            "document_ids": list(dict.fromkeys(
                chunk.document_id
                for chunk in rag_context.chunks
                if chunk.document_id
            )),
            "scores": [chunk.score for chunk in rag_context.chunks],
        }

        return {
            **result,
            "rag_audit": [audit_record],
        }

    return wrapped


# ═══════════════════════════════════════════════════════════════════════════
# NODE FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def build_all_nodes(
    session_factory,
    rag_adapter,
    client,
    outbound_dispatcher=None,
    structured_data=None,
    sales_context_service=None,
):
    M = _mods()
    ia   = M["ia"];   cat  = M["cat"]; req  = M["req"]
    lk   = M["lk"];   qual = M["qual"]
    inv  = M["inv"];   fe   = M["fe"]
    pd   = M["pd"];    pe   = M["pe"]
    qb   = M["qb"];    qr   = M["qr"]
    ft   = M["ft"];    od   = M["od"];  fc   = M["fc"]
    ne   = M["ne"];    rv   = M["rv"]
    poe  = M["poe"];   pov  = M["pov"]
    hb   = M["hb"];    hd   = M["hd"]

    if structured_data is None:
        from app.structured_documents import StructuredDataRepository
        structured_data = StructuredDataRepository(session_factory)

    def _draft(js): return qb.QuotationDraft(**json.loads(js)) if js else None
    def _pricing(d): return pe.PricingResult(**d) if d else None
    def _feasibility(d): return fe.FeasibilityResult(**d) if d else None
    def _qualification(d): return qual.QualificationResult(**d) if d else None

    async def _record_outgoing(
        state: CompletePipelineState,
        message: str,
        message_type: str,
        *,
        status: str = "sent",
        provider_message_id: str | None = None,
    ) -> None:
        from app.events.interactions import record_interaction
        from app.events.service import record_business_event

        async with session_factory() as session:
            interaction = await record_interaction(
                session,
                business_id=state["business_id"],
                customer_id=state.get("customer_id"),
                lead_id=state.get("lead_id"),
                thread_id=state.get("thread_id"),
                direction="outgoing",
                channel=state.get("outbound_channel") or state.get("source") or "email",
                message_type=message_type,
                recipient=(
                    state.get("outbound_recipient")
                    or state.get("sender_identifier")
                ),
                content=message,
                status=status,
                metadata={
                    "provider_message_id": provider_message_id,
                },
            )
            await record_business_event(
                session,
                business_id=state["business_id"],
                customer_id=state.get("customer_id"),
                lead_id=state.get("lead_id"),
                thread_id=state.get("thread_id"),
                event_type="message.sent",
                actor_id="sales_graph",
                entity_type="interaction",
                entity_id=interaction.id,
                data={"message_type": message_type},
            )
            await session.commit()

    async def _send_outbound(
        state: CompletePipelineState,
        message: str,
        message_type: str,
        *,
        subject: str,
        html: str | None = None,
    ):
        if outbound_dispatcher is None:
            raise RuntimeError(
                "Outbound dispatcher is not configured."
            )
        channel = state.get("outbound_channel") or "email"
        recipient = (
            state.get("outbound_recipient")
            or state.get("sender_identifier")
            or ""
        )
        result = await outbound_dispatcher.send(
            business_id=state["business_id"],
            channel=channel,
            recipient=recipient,
            subject=subject,
            text=message,
            html=html,
        )
        await _record_outgoing(
            state,
            message,
            message_type,
            status=result.status,
            provider_message_id=result.provider_message_id,
        )
        return result

    async def _record_transition(
        state: CompletePipelineState,
        event_type: str,
        actor_id: str,
        data: dict | None = None,
        entity_type: str = "pipeline",
        entity_id: str | None = None,
    ) -> None:
        from app.events.service import record_business_event

        async with session_factory() as session:
            await record_business_event(
                session,
                business_id=state["business_id"],
                customer_id=state.get("customer_id"),
                lead_id=state.get("lead_id"),
                thread_id=state.get("thread_id"),
                event_type=event_type,
                actor_id=actor_id,
                entity_type=entity_type,
                entity_id=entity_id,
                data=data,
            )
            await session.commit()

    def _contract_node(node_name: str, handler):
        """Add structured failures and persist the current business state."""
        async def wrapped(state: CompletePipelineState) -> dict:
            try:
                result = await handler(state)
            except Exception as exc:
                result = failure_result(node_name, exc)
            if result.get("error") and not result.get("failure"):
                result = {
                    **result,
                    **failure_result(node_name, result["error"]),
                }
            result.setdefault("current_node", node_name)
            result.setdefault(
                "status_updated_at",
                datetime.now(timezone.utc).isoformat(),
            )
            try:
                await persist_pipeline_snapshot(
                    session_factory, state, result, node_name
                )
            except Exception as exc:
                # Status persistence is authoritative for operations. If it
                # fails, stop rather than returning an invisible workflow.
                return failure_result(
                    node_name,
                    f"pipeline status persistence failed: {exc}",
                    code="PIPELINE_STATUS_PERSISTENCE_FAILED",
                )
            return result
        return wrapped


    async def _structured_document_context(
        state: CompletePipelineState,
        *,
        agent_name: str,
        documents: list[tuple[str, str]],
    ) -> AgentRAGContext:
        chunks = []
        for document_name, document_type in documents:
            context = await rag_adapter.get_document_context(
                agent_name=agent_name,
                state=state,
                document_name=document_name,
                document_type=document_type,
            )
            chunks.extend(context.chunks)

        return AgentRAGContext(
            agent_name=agent_name,
            query="Exact structured document retrieval",
            chunks=chunks,
        )



    def _rag_rows(
        rag_context: AgentRAGContext,
        document_type: str,
        document_name: str | None = None,
    ):
        """Yield complete pipe-delimited rows from retrieved CSV chunks."""
        seen: set[tuple[str, ...]] = set()
        for chunk in rag_context.chunks:
            if chunk.document_type != document_type:
                continue
            if (
                document_name
                and chunk.metadata.get("document_name") != document_name
            ):
                continue
            for line in chunk.text.splitlines():
                columns = tuple(part.strip() for part in line.split("|"))
                if columns and columns not in seen:
                    seen.add(columns)
                    yield columns

    def _inventory_from_rag(rag_context: AgentRAGContext):
        inventory_index = {}
        for row in _rag_rows(rag_context, "inventory"):
            if len(row) != 10 or row[0] == "product_code":
                continue
            try:
                item = inv.InventoryItem(
                    product_code=row[0],
                    product_name=row[1],
                    available_qty=float(row[5]),
                    unit="MT",
                    warehouse_location=row[2],
                    last_updated=row[9],
                )
            except (TypeError, ValueError):
                continue
            inventory_index[item.product_code] = item
        return inventory_index

    def _capacity_from_rag(rag_context: AgentRAGContext):
        capacity_index = {}
        for row in _rag_rows(rag_context, "production_capacity"):
            if len(row) != 10 or row[0] == "product_code":
                continue
            try:
                # The uploaded file exposes available daily capacity. The
                # feasibility engine expects available weekly capacity.
                available_weekly_capacity = float(row[5]) * 7
                capacity_index[row[0]] = {
                    "weekly_capacity_mt": available_weekly_capacity,
                    "lead_time_days": int(float(row[7])),
                    "min_order_qty_mt": 0.0,
                }
            except (TypeError, ValueError):
                continue
        return capacity_index

    def _delivery_from_rag(rag_context: AgentRAGContext):
        delivery_index = {}
        for row in _rag_rows(rag_context, "delivery_policy"):
            if len(row) != 11 or row[0] == "zone_code":
                continue
            try:
                delivery_index[row[1].lower()] = {
                    "zone": row[3],
                    "transit_days": int(float(row[6])),
                }
            except (TypeError, ValueError):
                  continue
        return delivery_index

    def _pricing_documents_from_rag(
        rag_context: AgentRAGContext,
    ):
        """Build the pricing engine's structured inputs from Qdrant chunks."""
        docs = pd.PricingDocuments()

        for row in _rag_rows(
            rag_context,
            "pricing_sheet",
            "price_list.csv",
        ):
            if len(row) != 10 or row[0] == "product_code":
                continue
            try:
                if row[9].lower() != "active":
                    continue
                docs.price_list[row[0]] = pd.PriceListEntry(
                    product_code=row[0],
                    base_price_per_mt=float(row[3]),
                    currency=row[4],
                    valid_until=row[6],
                )
            except (TypeError, ValueError):
                continue

        # Raw-material master data cannot be safely converted to a finished
        # product cost without a BOM. This normalized file must contain:
        # product_code, product_name, rm_cost_per_mt,
        # manufacturing_overhead_pct.
        for row in _rag_rows(
            rag_context,
            "pricing_sheet",
            "product_cost.csv",
        ):
            if len(row) != 4 or row[0] == "product_code":
                continue
            try:
                docs.rm_costs[row[0]] = pd.RMCostEntry(
                    product_code=row[0],
                    rm_cost_per_mt=float(row[2]),
                    manufacturing_overhead_pct=float(row[3]),
                )
            except (TypeError, ValueError):
                continue

        transport_by_zone: dict[str, list[float]] = {}
        for row in _rag_rows(
            rag_context,
            "pricing_sheet",
            "transport.csv",
        ):
            if len(row) != 12 or row[0] == "transport_rate_id":
                continue
            try:
                if row[11].lower() != "active":
                    continue
                transport_by_zone.setdefault(row[3], []).append(
                    float(row[6])
                )
            except (TypeError, ValueError):
                continue

        docs.transport_costs = {
            zone: round(sum(rates) / len(rates), 2)
            for zone, rates in transport_by_zone.items()
            if rates
        }

        # The pricing engine uses qualification values "new"/"existing".
        # Keep those exact values in this normalized policy file.
        for row in _rag_rows(
            rag_context,
            "discount_policy",
            "discount_policy_normalized.csv",
        ):
            if len(row) != 5 or row[0] == "customer_type":
                continue
            try:
                docs.discount_bands.append(
                    pd.DiscountBand(
                        customer_type=row[0],
                        order_value_min=float(row[1]),
                        order_value_max=float(row[2]),
                        max_discount_pct=float(row[3]),
                        approval_limit_pct=float(row[4]),
                    )
                )
            except (TypeError, ValueError):
                continue

        for row in _rag_rows(
            rag_context,
            "margin_policy",
            "margin_rules.csv",
        ):
            if len(row) != 10 or row[0] == "rule_id":
                continue
            try:
                if row[9].lower() != "active":
                    continue
                docs.margin_rules[row[1]] = pd.MarginRule(
                    product_code=row[1],
                    min_margin_pct=float(row[3]),
                    target_margin_pct=float(row[4]),
                )
            except (TypeError, ValueError):
                continue

        for row in _rag_rows(
            rag_context,
            "tax_policy",
            "gst_rates.csv",
        ):
            if len(row) != 10 or row[0] == "gst_rule_id":
                continue
            try:
                if row[9].lower() != "active":
                    continue
                docs.gst_rates[row[2]] = float(row[4])
            except (TypeError, ValueError):
                continue

        return docs

    # ══════════════════════════════════════════════════════════════════
    # ENTRY ROUTER — decides which sub-pipeline runs this invocation
    # ══════════════════════════════════════════════════════════════════

    async def check_trigger(state: CompletePipelineState) -> dict:
        """
        Refresh customer memory on every later event before routing. The first
        inquiry has no customer_id yet; it is loaded after identity resolution.
        """
        result = {"stages_completed": [f"trigger:{state['trigger']}"]}
        if sales_context_service is None or not state.get("customer_id"):
            return result
        agent_for_trigger = {
            "followup": "follow_up_management",
            "customer_reply": "negotiation_support",
            "po_received": "purchase_order_handling",
            "approved": "customer_qualification",
        }.get(state.get("trigger"), "customer_qualification")
        try:
            context = await sales_context_service.get_context(
                business_id=state["business_id"],
                customer_id=state["customer_id"],
                agent_name=agent_for_trigger,
                state=state,
            )
            result["customer_360"] = context.customer_360
            result["sales_context"] = context.model_dump()
            result["stages_completed"].append("sales_context_refreshed")
        except Exception as exc:
            result["error"] = f"sales context refresh failed: {exc}"
        return result

    # ══════════════════════════════════════════════════════════════════
    # SUB-PIPELINE A: INQUIRY → QUOTATION  (trigger="inquiry")
    # ══════════════════════════════════════════════════════════════════

    async def extract_inquiry(state: CompletePipelineState) -> dict:
        try:
            raw = ia.normalize_inquiry(
                ia.InquirySource(state["source"]),
                state["raw_text"],
                state.get("sender_identifier"),
            )
            extraction = ia.extract_inquiry(raw, client) if client else \
                ia.InquiryExtraction(inquiry_id=raw.inquiry_id,
                                     extraction_confidence=0.0,
                                     missing_fields=list(ia.REQUIRED_FIELDS))

            async with session_factory() as session:
                lead = await ia.create_lead(
                    session,
                    raw,
                    extraction,
                    business_id=state["business_id"],
                    thread_id=state["thread_id"],
                )

            return {
                "inquiry_id":       raw.inquiry_id,
                "lead_id":          lead.id,
                "extraction":       extraction.model_dump(),
                "needs_followup":   bool(extraction.missing_fields),
                "pipeline_status":  PipelineStatus.PROCESSING.value,
                "business_milestone": BusinessMilestone.INQUIRY_CAPTURED.value,
                "waiting_for": WaitingFor.NONE.value,
                "stages_completed": ["inquiry"],
            }
        except Exception as e:
            return {"error": f"extract_inquiry: {e}", "stages_completed": []}

    async def send_inquiry_followup(state: CompletePipelineState) -> dict:
        """Sends a follow-up asking for missing fields. Pipeline pauses."""
        try:
            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))
            raw = ia.normalize_inquiry(ia.InquirySource(state["source"]),
                                       state["raw_text"], state.get("sender_identifier"))
            msg = ia.compose_followup_message(ext, raw, client)
            if msg:
                await _send_outbound(
                    state,
                    msg.message_text,
                    "inquiry_followup",
                    subject="Additional information required",
                )
            async with session_factory() as session:
                await ia.log_action(session, "lead", state.get("lead_id",""),
                                    "missing_fields_followup_sent", "inquiry_agent",
                                    {"missing": ext.missing_fields})
                await session.commit()
            return {
                "outbound_message":  msg.message_text if msg else "",
                "pipeline_status":   PipelineStatus.AWAITING_CUSTOMER_INFORMATION.value,
                "waiting_for":       WaitingFor.CUSTOMER.value,
                "stages_completed":  ["inquiry_followup_sent"],
            }
        except Exception as e:
            return {"error": f"send_inquiry_followup: {e}", "stages_completed": []}


    async def normalize_requirement(state: CompletePipelineState) -> dict:
        try:
            normalized = normalize_product_requirement(
                state.get("extraction") or {},
                state.get("raw_text") or "",
            )
            return {
                "normalized_requirement": normalized,
                "product_resolution_status": "normalized",
                "pipeline_status": PipelineStatus.PROCESSING.value,
                "business_milestone": BusinessMilestone.REQUIREMENT_NORMALIZED.value,
                "waiting_for": WaitingFor.NONE.value,
                "stages_completed": ["requirement_normalized"],
            }
        except Exception as exc:
            return failure_result("normalize_requirement", exc)

    async def exact_product_code_lookup(state: CompletePipelineState) -> dict:
        try:
            normalized = state.get("normalized_requirement") or {}
            product_code = normalized.get("product_code")
            if not product_code:
                return {
                    "product_candidate": None,
                    "product_resolution_status": "semantic_fallback",
                    "stages_completed": ["exact_product_code_not_supplied"],
                }
            record = await structured_data.catalog_product(
                state["business_id"], product_code
            )
            if record is None:
                return {
                    "product_candidate": None,
                    "product_resolution_status": "semantic_fallback",
                    "stages_completed": ["exact_product_code_not_found"],
                }
            return {
                "product_candidate": {
                    "product_code": record.product_code,
                    "name": record.name,
                    "category": record.category,
                    "grade": record.grade,
                    "specifications": record.specifications,
                    "unit": record.unit,
                },
                "product_resolution_status": "candidate_found",
                "stages_completed": ["exact_product_code_found"],
            }
        except Exception as exc:
            return failure_result("exact_product_code_lookup", exc)

    async def structured_specification_match(state: CompletePipelineState) -> dict:
        try:
            candidate_data = state.get("product_candidate")
            if not candidate_data:
                return {
                    "product_resolution_status": "semantic_fallback",
                    "stages_completed": ["structured_product_unavailable"],
                }
            candidate = cat.CatalogProduct(**candidate_data)
            verification = verify_structured_product(
                state.get("normalized_requirement") or {}, candidate
            )
            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))
            if verification["exact"]:
                result = req.RequirementSummary(
                    inquiry_id=ext.inquiry_id,
                    match_type=req.MatchType.EXACT,
                    matched_product=candidate,
                    similarity_score=1.0,
                    gap_analysis=None,
                    requires_human_review=False,
                    summary_text=(
                        f"Exact structured catalog match: {candidate.product_code} "
                        f"— {candidate.name}. Product code and all explicitly "
                        "requested structured specifications were verified."
                    ),
                )
                return {
                    "requirement": result.model_dump(),
                    "product_resolution_status": "exact",
                    "business_milestone": BusinessMilestone.PRODUCT_RESOLVED.value,
                    "stages_completed": ["structured_product_exact"],
                }
            mismatches = verification["mismatches"]
            result = req.RequirementSummary(
                inquiry_id=ext.inquiry_id,
                match_type=req.MatchType.NEAR,
                matched_product=candidate,
                similarity_score=1.0,
                gap_analysis=req.GapAnalysis(
                    gaps=[
                        f"{item['field']}: requested {item['requested']}; "
                        f"catalog has {item['catalog_value']}"
                        for item in mismatches
                    ],
                    critical_gap=True,
                    can_fulfill=False,
                    notes="Exact code found, but structured specifications conflict.",
                ),
                requires_human_review=True,
                human_review_reason="Product code exists but mandatory specifications conflict.",
                summary_text=(
                    f"Product code {candidate.product_code} was found, but grade, "
                    "standard, size, or class requires technical confirmation."
                ),
            )
            return {
                "requirement": result.model_dump(),
                "product_resolution_status": "technical_review",
                "needs_human_approval": True,
                "human_approval_stage": "requirement",
                "human_approval_reasons": [result.human_review_reason],
                "stages_completed": ["structured_product_conflict"],
            }
        except Exception as exc:
            return failure_result("structured_specification_match", exc)

# for product match 
    async def match_requirement(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            ext = ia.InquiryExtraction(
                **(state.get("extraction") or {})
            )
            requested = " ".join(filter(None, [ext.product_requested, ext.specifications]))
            code_match = re.search(r"\b[A-Z]{2,8}-\d{2,8}\b", requested.upper())
            exact = await structured_data.catalog_product(
                state["business_id"], code_match.group(0)
            ) if code_match else None
            if exact:
                product = cat.CatalogProduct(
                    product_code=exact.product_code, name=exact.name,
                    category=exact.category, grade=exact.grade,
                    specifications=exact.specifications, unit=exact.unit,
                )
                result = req.RequirementSummary(
                    inquiry_id=ext.inquiry_id, match_type=req.MatchType.EXACT,
                    matched_product=product, similarity_score=1.0,
                    needs_technical_doc_review=req.detect_technical_doc_reference(ext),
                    requires_human_review=False,
                    summary_text=f"Exact catalog product {product.product_code} ({product.name}) verified in PostgreSQL.",
                    evidence_chunk_ids=rag_context.chunk_ids,
                    retrieval_query=rag_context.query,
                )
            else:
                result = req.match_requirement(
                    extraction=ext, client=client, rag_context=rag_context,
                )
                if result.matched_product:
                    verified = await structured_data.catalog_product(
                        state["business_id"], result.matched_product.product_code
                    )
                    if verified is None:
                        result.matched_product = None
                        result.match_type = req.MatchType.CUSTOM
                        result.requires_human_review = True
                        result.human_review_reason = "Semantic candidate failed exact PostgreSQL catalog verification."
            return {
                "requirement": result.model_dump(),
                "needs_human_approval": result.requires_human_review,
                "human_approval_stage": (
                    "requirement" if result.requires_human_review else None
                ),
                "human_approval_reasons": (
                    [result.human_review_reason]
                    if result.human_review_reason else []
                ),
                "product_resolution_status": (
                    "technical_review" if result.requires_human_review else "semantic_verified"
                ),
                "business_milestone": BusinessMilestone.PRODUCT_RESOLVED.value,
                "stages_completed": ["requirement"],
            }
        except Exception as exc:
            return {
                "error": f"match_requirement: {exc}",
                "stages_completed": [],
            }

#for customer match         

    async def resolve_customer_identity(state: CompletePipelineState) -> dict:
        try:
            from app.customers.identity_resolver import (
                resolve_customer_identity as resolve_identity,
            )
            from sqlalchemy import update

            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))
            async with session_factory() as session:
                resolution = await resolve_identity(
                    session,
                    business_id=state["business_id"],
                    lead_id=state.get("lead_id"),
                    company_name=ext.company_name,
                    contact_person=ext.contact_person,
                    email=ext.customer_email,
                    phone=ext.customer_phone,
                    gstin=ext.customer_gstin,
                    sender_identifier=state.get("sender_identifier"),
                )
                await session.execute(
                    update(ia.Lead)
                    .where(ia.Lead.id == state.get("lead_id"))
                    .values(customer_id=resolution.customer.id)
                )
                from app.events.service import record_business_event
                await record_business_event(
                    session,
                    business_id=state["business_id"],
                    customer_id=resolution.customer.id,
                    lead_id=state.get("lead_id"),
                    thread_id=state.get("thread_id"),
                    event_type="customer.resolved",
                    actor_id="customer_identity_resolver",
                    entity_type="customer",
                    entity_id=resolution.customer.id,
                    data={
                        "resolution": resolution.resolution,
                        "confidence": resolution.confidence,
                        "review_id": resolution.review_id,
                    },
                )
                await session.commit()

            return {
                "customer_id": resolution.customer.id,
                "customer_resolution": {
                    "resolution": resolution.resolution,
                    "confidence": resolution.confidence,
                    "matched_signals": resolution.matched_signals,
                    "conflicting_signals": resolution.conflicting_signals,
                },
                "customer_match_review_id": resolution.review_id,
                "stages_completed": ["customer_identity_resolved"],
            }
        except Exception as exc:
            return {
                "error": f"resolve_customer_identity: {exc}",
                "stages_completed": [],
            }

    async def load_customer_360(state: CompletePipelineState) -> dict:
        try:
            if sales_context_service is not None:
                sales_context = await sales_context_service.get_context(
                    business_id=state["business_id"],
                    customer_id=state["customer_id"],
                    agent_name="customer_qualification",
                    state=state,
                )
                customer_360 = sales_context.customer_360
                sales_context_data = sales_context.model_dump()
            else:
                from app.customers.customer_360 import get_customer_360
                async with session_factory() as session:
                    customer_360 = await get_customer_360(
                        session,
                        business_id=state["business_id"],
                        customer_id=state["customer_id"],
                    )
                sales_context_data = None
            return {
                "customer_360": customer_360,
                "sales_context": sales_context_data,
                "stages_completed": ["customer_360_loaded"],
            }
        except Exception as exc:
            return {
                "error": f"load_customer_360: {exc}",
                "stages_completed": [],
            }

    async def qualify_customer(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))

            async with session_factory() as session:
                profile = await lk.build_customer_profile(
                    session,
                    customer_id=state["customer_id"],
                    business_id=state["business_id"],
                    is_new=(
                        (state.get("customer_resolution") or {}).get("resolution")
                        in {"created", "needs_review"}
                    ),
                )

            result = qual.qualify_lead(
                ext.inquiry_id,
                profile,
                client,
                rag_context=rag_context,
                customer_360=state.get("customer_360"),
                sales_context=state.get("sales_context"),
            )
            await _record_transition(
                state,
                "qualification.completed",
                "qualification_agent",
                {
                    "credit_risk": result.credit_risk_flag,
                    "customer_360_summary": (
                        state.get("customer_360") or {}
                    ).get("summary", {}),
                },
                "lead",
                state.get("lead_id"),
            )

            return {
                "customer_id": profile.customer_id,
                "customer_profile": profile.model_dump(),
                "qualification": result.model_dump(),
                "needs_human_approval": result.credit_risk_flag,
                "human_approval_stage": (
                    "qualification" if result.credit_risk_flag else None
                ),
                "human_approval_reasons": (
                    [result.credit_risk_reason]
                    if result.credit_risk_flag and result.credit_risk_reason
                    else []
                ),
                "stages_completed": ["qualification"],
            }
        except Exception as e:
            return {"error": f"qualify_customer: {e}", "stages_completed": []}

    async def check_feasibility(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))
            req_ = _rebuild_requirement(
                state.get("requirement") or {},
                req,
                cat,
            )

            ql = _qualification(state.get("qualification"))

            inventory_rows, capacity_index, delivery_index = (
                await structured_data.feasibility_indexes(state["business_id"])
            )
            inventory_index = {
                code: inv.InventoryItem(
                    product_code=code,
                    product_name=data["product_name"],
                    available_qty=data["available_qty"],
                    unit="MT",
                    warehouse_location=", ".join(data["warehouses"]),
                    last_updated=data["last_updated"],
                )
                for code, data in inventory_rows.items()
            }

            if not inventory_index:
                raise ValueError(
                    "No active inventory rows were found in PostgreSQL"
                )

            inventory_result = inv.check_inventory(
                req_,
                ext,
                inventory_index,
            )

            result = fe.check_feasibility(
                extraction=ext,
                requirement=req_,
                qualification=ql,
                inventory=inventory_result,
                capacity_index=capacity_index,
                delivery_index=delivery_index,
                client=client,
                rag_context=rag_context,
            )
            await _record_transition(
                state,
                "feasibility.completed",
                "feasibility_agent",
                {"requires_human_review": result.requires_human_review},
                "lead",
                state.get("lead_id"),
            )

            return {
                "feasibility": result.model_dump(),
                "needs_human_approval": result.requires_human_review,
                "human_approval_stage": (
                    "feasibility" if result.requires_human_review else None
                ),
                "human_approval_reasons": result.human_review_reasons,
                "stages_completed": ["feasibility"],
            }
        except Exception as e:
            return {"error": f"check_feasibility: {e}", "stages_completed": []}

    async def compute_pricing(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))
            req_ = _rebuild_requirement(
                state.get("requirement") or {},
                req,
                cat,
            )
            ql = _qualification(state.get("qualification"))
            fs = _feasibility(state.get("feasibility"))
            rows = await structured_data.pricing_documents(
                state["business_id"]
            )
            product_category = (
                req_.matched_product.category if req_.matched_product else ""
            )
            product_code = (
                req_.matched_product.product_code if req_.matched_product else ""
            )
            docs = pd.PricingDocuments(
                price_list={row.product_code: pd.PriceListEntry(
                    row.product_code, float(row.base_price_inr), row.currency,
                    row.effective_to.isoformat() if row.effective_to else "",
                ) for row in rows["prices"] if row.status.lower() == "active"},
                rm_costs={row.product_code: pd.RMCostEntry(
                    row.product_code, float(row.rm_cost_per_mt),
                    float(row.manufacturing_overhead_pct),
                ) for row in rows["costs"]},
                transport_costs={
                    row.zone: float(row.rate_per_mt_inr)
                    for row in rows["transport"] if row.status.lower() == "active"
                },
                discount_bands=[pd.DiscountBand(
                    row.customer_type, float(row.order_value_min),
                    float(row.order_value_max), float(row.max_discount_pct),
                    float(row.approval_limit_pct),
                ) for row in rows["discounts"]],
                margin_rules={row.product_code: pd.MarginRule(
                    row.product_code, float(row.minimum_margin_pct),
                    float(row.target_margin_pct),
                ) for row in rows["margins"]
                  if row.product_code and row.status.lower() == "active"},
                gst_rates={
                    (row.product_category or (
                        product_category if row.product_code == product_code else ""
                    )): float(row.gst_rate_pct)
                    for row in rows["gst"]
                    if row.status.lower() == "active"
                    and (row.product_category or row.product_code)
                },
            )

            result = pe.compute_pricing(
                extraction=ext,
                requirement=req_,
                qualification=ql,
                feasibility=fs,
                docs=docs,
                client=client,
                rag_context=rag_context,
            )
            await _record_transition(
                state,
                "pricing.completed",
                "pricing_agent",
                {
                    "pricing_possible": result.pricing_possible,
                    "requires_human_approval": (
                        result.requires_human_approval
                    ),
                },
                "lead",
                state.get("lead_id"),
            )

            if not result.pricing_possible:
                return {
                    "pricing": result.model_dump(),
                    "pipeline_status": PipelineStatus.BLOCKED.value,
                    "waiting_for": WaitingFor.MASTER_DATA_ADMIN.value,
                    "status_reason": "; ".join(result.approval_reasons),
                    "failure": {
                        "category": "missing_master_data",
                        "code": "PRICING_DATA_MISSING",
                        "message": "Required deterministic pricing data is missing.",
                        "node": "compute_pricing",
                        "retryable": True,
                        "details": {"missing_inputs": result.approval_reasons},
                    },
                    "needs_human_approval": False,
                    "human_approval_stage": None,
                    "human_approval_reasons": result.approval_reasons,
                    "stages_completed": ["pricing_blocked"],
                }

            return {
                "pricing": result.model_dump(),
                "needs_human_approval": result.requires_human_approval,
                "human_approval_stage": (
                    "pricing" if result.requires_human_approval else None
                ),
                "human_approval_reasons": result.approval_reasons,
                "stages_completed": ["pricing"],
            }
        except Exception as e:
            return {"error": f"compute_pricing: {e}", "stages_completed": []}

    async def generate_quotation(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))
            ql = _qualification(state.get("qualification"))
            fs = _feasibility(state.get("feasibility"))
            pr = _pricing(state.get("pricing"))
            cust = lk.CustomerProfile(
                **(state.get("customer_profile") or {})
            )

            payment_rule = await structured_data.payment_term(
                state["business_id"],
                (
                    (state.get("customer_360") or {})
                    .get("customer", {})
                    .get("customer_type")
                    or ql.customer_type
                ),
                pr.total_invoice_value,
            )
            payment_terms = None
            if payment_rule:
                payment_terms = (
                    payment_rule.term_id,
                    f"{float(payment_rule.advance_percentage):g}% advance; "
                    f"{payment_rule.balance_payment_condition or 'balance as agreed'}; "
                    f"credit {payment_rule.credit_days} days.",
                )

            draft = qb.build_quotation(
                extraction=ext,
                pricing=pr,
                feasibility=fs,
                qualification=ql,
                customer=cust,
                rag_context=rag_context,
                payment_terms=payment_terms,
            )
            html = qr.render_quotation_html(draft)

            async with session_factory() as session:
                rec = await qr.save_quotation(
                    session,
                    draft,
                    html,
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    thread_id=state["thread_id"],
                )
                if (
                    rec.status == qr.QuotationStatus.PENDING_APPROVAL
                    and state.get("approved_stage") == "pricing"
                ):
                    rec.status = qr.QuotationStatus.APPROVED
                    rec.approved_by = "pipeline_approval"
                    await session.commit()

            quotation_message = (
                f"Please find quotation {draft.quotation_number} for "
                f"{draft.buyer_company}. Total value: "
                f"₹{draft.total_inc_gst:,.2f}. Valid until "
                f"{draft.valid_until}."
            )
            await _record_transition(
                state,
                "quotation.created",
                "quotation_agent",
                {"quotation_number": draft.quotation_number},
                "quotation",
                rec.id,
            )

            return {
                "quotation_id": rec.id,
                "quotation_number": draft.quotation_number,
                "final_draft_json": draft.model_dump_json(),
                "quotation_html": html,
                "quotation_message": quotation_message,
                "pipeline_status": PipelineStatus.QUOTATION_DISPATCH_PENDING.value,
                "business_milestone": BusinessMilestone.QUOTATION_CREATED.value,
                "waiting_for": WaitingFor.EXTERNAL_SYSTEM.value,
                "stages_completed": ["quotation_created"],
            }
        except Exception as e:
            return {"error": f"generate_quotation: {e}", "stages_completed": []}

    # ══════════════════════════════════════════════════════════════════
    # SUB-PIPELINE B: FOLLOW-UP  (trigger="followup")
    # ══════════════════════════════════════════════════════════════════

    async def dispatch_quotation(state: CompletePipelineState) -> dict:
        """Send a persisted quotation once and only then mark it as sent."""
        from app.database.models import QuotationDeliveryAttempt

        quotation_id = state.get("quotation_id")
        channel = state.get("outbound_channel") or "email"
        recipient = (
            state.get("outbound_recipient")
            or state.get("sender_identifier")
            or ""
        )
        if not quotation_id or not recipient:
            return failure_result(
                "dispatch_quotation",
                ValueError("Quotation id and recipient are required for dispatch."),
            )

        async with session_factory() as session:
            attempt = await session.scalar(
                select(QuotationDeliveryAttempt).where(
                    QuotationDeliveryAttempt.business_id == state["business_id"],
                    QuotationDeliveryAttempt.quotation_id == quotation_id,
                    QuotationDeliveryAttempt.channel == channel,
                    QuotationDeliveryAttempt.recipient == recipient,
                )
            )
            if attempt and attempt.status == "sent":
                sent_at = attempt.sent_at or datetime.now(timezone.utc)
                return {
                    "quotation_delivery_id": attempt.id,
                    "quotation_sent_at": sent_at.isoformat(),
                    "pipeline_status": PipelineStatus.AWAITING_CUSTOMER_REPLY.value,
                    "business_milestone": BusinessMilestone.QUOTATION_SENT.value,
                    "waiting_for": WaitingFor.CUSTOMER.value,
                    "stages_completed": ["quotation_delivery_already_confirmed"],
                }
            if attempt is None:
                attempt = QuotationDeliveryAttempt(
                    business_id=state["business_id"], quotation_id=quotation_id,
                    thread_id=state["thread_id"], channel=channel,
                    recipient=recipient,
                )
                session.add(attempt)
            attempt.status = "sending"
            attempt.attempt_count = (attempt.attempt_count or 0) + 1
            await session.commit()
            delivery_id = attempt.id

        try:
            delivery = await _send_outbound(
                state,
                state.get("quotation_message") or "",
                "quotation",
                subject=f"Quotation {state.get('quotation_number', '')}",
                html=state.get("quotation_html"),
            )
            sent_at = datetime.now(timezone.utc)
            async with session_factory() as session:
                attempt = await session.get(QuotationDeliveryAttempt, delivery_id)
                stored = await session.get(qr.QuotationRecord, quotation_id)
                attempt.status = "sent"
                attempt.provider_message_id = delivery.provider_message_id
                attempt.sent_at = sent_at
                attempt.last_error = None
                stored.status = qr.QuotationStatus.SENT
                stored.sent_at = sent_at
                stored.sent_via = channel
                stored.sent_to = recipient
                from app.followups.service import schedule_quotation_followups
                await schedule_quotation_followups(
                    session,
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    lead_id=state.get("lead_id"),
                    thread_id=state["thread_id"],
                    quotation_id=stored.id,
                    quotation_number=stored.quotation_number,
                    sent_at=sent_at,
                    channel=channel,
                    recipient=recipient,
                )
                await session.commit()
            await _record_transition(
                state, "quotation.sent", "quotation_agent",
                {"provider_message_id": delivery.provider_message_id},
                "quotation", quotation_id,
            )
            return {
                "quotation_delivery_id": delivery_id,
                "quotation_sent_at": sent_at.isoformat(),
                "pipeline_status": PipelineStatus.AWAITING_CUSTOMER_REPLY.value,
                "business_milestone": BusinessMilestone.QUOTATION_SENT.value,
                "waiting_for": WaitingFor.CUSTOMER.value,
                "stages_completed": ["quotation_dispatched"],
            }
        except Exception as exc:
            async with session_factory() as session:
                attempt = await session.get(QuotationDeliveryAttempt, delivery_id)
                if attempt:
                    attempt.status = "failed"
                    attempt.last_error = str(exc)
                    await session.commit()
            return failure_result("dispatch_quotation", exc)

    async def compose_reminder(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            draft    = _draft(state.get("final_draft_json"))
            attempt  = state.get("followup_attempt", 1)
            schedule = ft.FOLLOW_UP_SCHEDULE[min(attempt - 1, 3)]

            if draft is None:
                return {"error": "compose_reminder: no draft", "stages_completed": []}

            msg = fc.generate_reminder(
                draft,
                schedule,
                state.get("outbound_channel", "email"),
                client,
                rag_context=rag_context,
            )
            return {
                "followup_message":  msg,
                "followup_tone":     schedule.tone,
                "outbound_message":  msg,
                "stages_completed":  [f"reminder_{schedule.tone}"],
            }
        except Exception as e:
            return {"error": f"compose_reminder: {e}", "stages_completed": []}

    async def dispatch_followup(state: CompletePipelineState) -> dict:
        try:
            msg       = state.get("outbound_message","")
            delivery = await _send_outbound(
                state,
                msg,
                "followup",
                subject=(
                    f"Follow-up: quotation "
                    f"{state.get('quotation_number', '')}"
                ),
            )
            channel = state.get("outbound_channel", "email")
            recipient = state.get("outbound_recipient", "")
            async with session_factory() as session:
                draft = _draft(state.get("final_draft_json"))
                schedule = ft.FOLLOW_UP_SCHEDULE[min(state.get("followup_attempt",1)-1,3)]
                record = await ft.create_followup_record(
                    session,
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    thread_id=state["thread_id"],
                    quotation_id=state.get("quotation_id",""),
                    quotation_number=state.get("quotation_number",""),
                    inquiry_id=state.get("inquiry_id",""),
                    buyer_company=draft.buyer_company if draft else "",
                    channel=channel, recipient=recipient,
                    attempt=state.get("followup_attempt",1),
                    followup_type=schedule.followup_type,
                    tone=schedule.tone,
                    message_text=msg,
                )
            return {
                "pipeline_status":    PipelineStatus.AWAITING_CUSTOMER_REPLY.value,
                "business_milestone": BusinessMilestone.FOLLOWUP_SENT.value,
                "waiting_for":        WaitingFor.CUSTOMER.value,
                "followup_attempt":   state.get("followup_attempt",1) + 1,
                "followup_record_id": record.id,
                "followup_provider_message_id": (
                    delivery.provider_message_id
                ),
                "stages_completed":   ["followup_dispatched"],
            }
        except Exception as e:
            return {"error": f"dispatch_followup: {e}", "stages_completed": []}

    # ══════════════════════════════════════════════════════════════════
    # SUB-PIPELINE C: CUSTOMER REPLY  (trigger="customer_reply")
    # ══════════════════════════════════════════════════════════════════

    async def analyze_reply(state: CompletePipelineState) -> dict:
        """
        Uses Gemini to classify the customer reply into one of:
          counter_offer | objection | positive | po_text
        """
        try:
            from app.followups.service import (
                cancel_open_followup_jobs,
            )

            async with session_factory() as session:
                await cancel_open_followup_jobs(
                    session,
                    business_id=state["business_id"],
                    thread_id=state["thread_id"],
                    reason="Customer replied.",
                )
                await session.commit()

            reply = state.get("customer_reply_text","")
            if not reply:
                return {"reply_type":"unknown","stages_completed":["reply_analyzed"]}

            # Check if it looks like a PO document
            po_signals = ["purchase order","p.o.","po no","po number","po date",
                          "kindly supply","please supply","we hereby place"]
            if any(sig in reply.lower() for sig in po_signals):
                return {"reply_type":"po_text",
                        "po_raw_text": reply,
                        "stages_completed":["reply_classified_as_po"]}

            acceptance_signals = (
                "we accept", "accepted", "commercials approved", "go ahead",
                "please proceed", "po will follow", "send bank details",
            )
            if any(signal in reply.lower() for signal in acceptance_signals):
                return {
                    "reply_type": "commercial_acceptance",
                    "stages_completed": ["commercial_acceptance_detected"],
                }

            # Use objection detector to classify
            analysis = od.detect_objection(reply, client)

            if analysis.objection_type.value == "positive_interest":
                return {"reply_type":"positive",
                        "objection": analysis.model_dump(),
                        "stages_completed":["reply_positive_interest"]}

            if analysis.customer_price_mentioned:
                return {"reply_type":"counter_offer",
                        "objection": analysis.model_dump(),
                        "stages_completed":["reply_counter_offer_detected"]}

            return {"reply_type":"objection",
                    "objection": analysis.model_dump(),
                    "stages_completed":["reply_objection_detected"]}

        except Exception as e:
            return {"error": f"analyze_reply: {e}",
                    "reply_type":"unknown", "stages_completed":[]}

    async def record_commercial_acceptance(state: CompletePipelineState) -> dict:
        return {
            "outbound_message": (
                "Thank you for accepting the commercial terms. "
                "Please send the purchase order so we can validate current price, "
                "stock, capacity, credit, and delivery before confirming the order."
            ),
            "pipeline_status": PipelineStatus.AWAITING_PURCHASE_ORDER.value,
            "business_milestone": BusinessMilestone.COMMERCIALS_ACCEPTED.value,
            "waiting_for": WaitingFor.CUSTOMER.value,
            "stages_completed": ["commercials_accepted_awaiting_po"],
        }

    async def compose_positive_response(state: CompletePipelineState) -> dict:
        return {
            "outbound_message": (
                "Thank you for your interest. Please let us know if you would like "
                "to accept the quotation or need any clarification."
            ),
            "pipeline_status": PipelineStatus.AWAITING_CUSTOMER_REPLY.value,
            "waiting_for": WaitingFor.CUSTOMER.value,
            "stages_completed": ["positive_interest_acknowledged"],
        }

    async def evaluate_counteroffer(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            pricing = _pricing(state.get("pricing"))
            obj = od.ObjectionAnalysis(
                **(state.get("objection") or {})
            )
            customer_price = obj.customer_price_mentioned or 0.0

            if not pricing or customer_price <= 0:
                return {
                    "error": "evaluate_counteroffer: no pricing or price",
                    "stages_completed": [],
                }

            analysis = ne.evaluate_counteroffer(
                customer_price_per_mt=customer_price,
                pricing=pricing,
                rag_context=rag_context,
                decision_context={
                    "credit_limit": float(
                        ((state.get("customer_360") or {}).get("customer") or {}).get("credit_limit") or 0
                    ),
                    "credit_exposure_after_order": float(
                        ((state.get("customer_360") or {}).get("customer") or {}).get("outstanding_amount") or 0
                    ) + customer_price * pricing.quantity_mt,
                    "available_stock": float(
                        (state.get("feasibility") or {}).get("stock_qty") or 0
                    ),
                    "production_utilization_pct": (
                        (state.get("feasibility") or {}).get("production_utilization_pct")
                    ),
                    "policy_version": "current_postgresql_pricing_snapshot",
                },
            )

            return {
                "negotiation_analysis": analysis.model_dump(),
                "needs_human_approval": analysis.requires_human_approval,
                "human_approval_stage": (
                    "negotiation"
                    if analysis.requires_human_approval
                    else None
                ),
                "human_approval_reasons": (
                    [analysis.human_approval_reason]
                    if analysis.human_approval_reason
                    else []
                ),
                "pipeline_status": PipelineStatus.PROCESSING.value,
                "waiting_for": WaitingFor.NONE.value,
                "stages_completed": ["counteroffer_evaluated"],
            }
        except Exception as e:
            return {"error": f"evaluate_counteroffer: {e}", "stages_completed": []}

    async def prepare_revised_quotation(state: CompletePipelineState) -> dict:
        try:
            draft    = _draft(state.get("final_draft_json"))
            analysis = ne.NegotiationAnalysis(**(state.get("negotiation_analysis") or {}))
            version  = state.get("negotiation_version", 1) + 1

            revised  = rv.build_revised_draft(draft, analysis, version, "negotiation_agent")
            html     = qr.render_quotation_html(revised)

            async with session_factory() as session:
                ver_rec = await rv.save_quotation_version(
                    session,
                    state.get("quotation_id",""),
                    state.get("quotation_number",""), revised, analysis,
                    "customer_counteroffer_accepted", "negotiation_agent",
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    thread_id=state["thread_id"],
                )
            return {
                "revised_draft_json":  revised.model_dump_json(),
                "final_draft_json":    revised.model_dump_json(),   # update latest
                "negotiation_version": version,
                "outbound_message":    (
                    f"Dear {revised.buyer_contact or 'Sir/Madam'},\n\n"
                    f"Please find revised Quotation {revised.quotation_number} "
                    f"at ₹{analysis.customer_price_per_mt:,.0f}/MT as discussed.\n\n"
                    f"Total: ₹{revised.total_inc_gst:,.0f} (incl. GST). "
                    f"Valid until {revised.valid_until}.\n\nRegards."
                ),
                "stages_completed": [f"revised_quotation_v{version}"],
            }
        except Exception as e:
            return {"error": f"prepare_revised_quotation: {e}", "stages_completed": []}

    async def compose_rejection(state: CompletePipelineState) -> dict:
        try:
            analysis = ne.NegotiationAnalysis(**(state.get("negotiation_analysis") or {}))
            draft    = _draft(state.get("final_draft_json"))
            counter  = analysis.counter_proposal_per_mt or analysis.floor_price_per_mt
            msg = (
                f"Dear {draft.buyer_contact if draft else 'Sir/Madam'},\n\n"
                f"Thank you for your counter-offer. Unfortunately we are unable to "
                f"match ₹{analysis.customer_price_per_mt:,.0f}/MT as it falls below "
                f"our minimum cost threshold.\n\n"
                f"Our best possible price is ₹{counter:,.0f}/MT (ex-GST). "
                f"Please let us know if this works for you.\n\nRegards."
            )
            return {
                "outbound_message": msg,
                "pipeline_status":  PipelineStatus.AWAITING_CUSTOMER_REPLY.value,
                "waiting_for": WaitingFor.CUSTOMER.value,
                "stages_completed": ["rejection_composed"],
            }
        except Exception as e:
            return {"error": f"compose_rejection: {e}", "stages_completed": []}

    async def compose_objection_response(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            draft    = _draft(state.get("final_draft_json"))
            obj      = od.ObjectionAnalysis(**(state.get("objection") or {}))
            pricing  = _pricing(state.get("pricing"))

            suggestion = (
                od.suggest_negotiation(
                    obj,
                    pricing,
                    client,
                    rag_context=rag_context,
                )
                if pricing
                else None
            )

            if suggestion and draft:
                msg = fc.generate_objection_response(
                    draft, obj, suggestion,
                    state.get("outbound_channel","email"), client
                )
            else:
                msg = (f"Dear Customer,\n\nThank you for your feedback regarding "
                       f"quotation {state.get('quotation_number','')}. "
                       f"We understand your concern about {obj.key_concern}. "
                       f"Please allow us to address this.\n\nRegards.")
            return {
                "outbound_message":  msg,
                "pipeline_status":   PipelineStatus.AWAITING_CUSTOMER_REPLY.value,
                "waiting_for": WaitingFor.CUSTOMER.value,
                "stages_completed":  ["objection_response_composed"],
            }
        except Exception as e:
            return {"error": f"compose_objection_response: {e}", "stages_completed": []}

    async def dispatch_message(state: CompletePipelineState) -> dict:
        """Shared dispatch node — used by follow-up, negotiation, rejection, objection."""
        try:
            msg       = state.get("outbound_message","")
            await _send_outbound(
                state,
                msg,
                "pipeline_message",
                subject=(
                    f"Regarding quotation "
                    f"{state.get('quotation_number', '')}"
                ),
            )
            channel = state.get("outbound_channel", "email")
            recipient = state.get("outbound_recipient", "")
            async with session_factory() as session:
                await ia.log_action(session, "pipeline", state.get("inquiry_id",""),
                                    "message_dispatched", "pipeline",
                                    {"channel":channel,"recipient":recipient,
                                     "pipeline_status": state.get("pipeline_status")})
                await session.commit()
            return {"stages_completed": ["message_dispatched"]}
        except Exception as e:
            return {"error": f"dispatch_message: {e}", "stages_completed": []}

    # ══════════════════════════════════════════════════════════════════
    # SUB-PIPELINE D: PO RECEIVED  (trigger="po_received")
    # ══════════════════════════════════════════════════════════════════

    async def extract_po_fields(state: CompletePipelineState) -> dict:
        try:
            po_text  = state.get("po_raw_text","")
            ext      = poe.extract_po_fields(po_text, client)
            async with session_factory() as session:
                po_rec = await poe.save_po_to_db(
                    session, ext,
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    thread_id=state["thread_id"],
                    quotation_id=state.get("quotation_id"),
                    quotation_number=state.get("quotation_number"),
                    inquiry_id=state.get("inquiry_id"),
                )
                from app.followups.service import (
                    cancel_open_followup_jobs,
                )

                await cancel_open_followup_jobs(
                    session,
                    business_id=state["business_id"],
                    thread_id=state["thread_id"],
                    reason="Purchase order received.",
                )
                await session.commit()
            return {
                "po_id":          po_rec.id,
                "po_extraction":  ext.model_dump(),
                "pipeline_status": PipelineStatus.PROCESSING.value,
                "business_milestone": BusinessMilestone.PO_RECEIVED.value,
                "waiting_for": WaitingFor.NONE.value,
                "stages_completed":["po_extracted"],
            }
        except Exception as e:
            return {"error": f"extract_po_fields: {e}", "stages_completed": []}

    async def validate_po(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            from app.database.models import QuotationRecord
            async with session_factory() as session:
                stored_quote = await session.scalar(
                    select(QuotationRecord).where(
                        QuotationRecord.id == state.get("quotation_id"),
                        QuotationRecord.business_id == state["business_id"],
                        QuotationRecord.thread_id == state["thread_id"],
                    )
                )
            draft = _draft(
                stored_quote.draft_json
                if stored_quote else state.get("final_draft_json")
            )
            ext = poe.POExtraction(
                **(state.get("po_extraction") or {})
            )

            if not draft:
                return {
                    "error": "validate_po: no draft",
                    "stages_completed": [],
                }

            result = pov.validate_po(
                po=ext,
                draft=draft,
                rag_context=rag_context,
            )

            async with session_factory() as session:
                await ia.log_action(
                    session,
                    "purchase_order",
                    state.get("po_id", ""),
                    "po_validated",
                    "purchase_order_agent",
                    {
                        "verdict": result.verdict.value,
                        "critical": result.critical_count,
                        "evidence_chunk_ids": rag_context.chunk_ids,
                    },
                )
                await session.commit()

            return {
                "po_validation": result.model_dump(),
                "po_verdict": result.verdict.value,
                "needs_human_approval": (
                    result.requires_human_review
                    and not result.requires_customer_correction
                ),
                "human_approval_stage": (
                    "po"
                    if (
                        result.requires_human_review
                        and not result.requires_customer_correction
                    )
                    else None
                ),
                "outbound_message": (
                    result.customer_correction_message or ""
                ),
                "stages_completed": ["po_validated"],
            }
        except Exception as e:
            return {"error": f"validate_po: {e}", "stages_completed": []}

    async def revalidate_order_state(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        """Refresh every time-sensitive decision before accepting a PO."""
        try:
            fresh_feasibility = await check_feasibility(state, rag_context)
            if fresh_feasibility.get("error"):
                return fresh_feasibility
            pricing_state = {**state, **fresh_feasibility}
            fresh_pricing = await compute_pricing(pricing_state, rag_context)
            if fresh_pricing.get("error"):
                return fresh_pricing

            old_pricing = state.get("pricing") or {}
            new_pricing = fresh_pricing.get("pricing") or {}
            old_price = float(old_pricing.get("final_price_per_mt_ex_gst") or 0)
            new_price = float(new_pricing.get("final_price_per_mt_ex_gst") or 0)
            price_change_pct = (
                abs(new_price - old_price) * 100 / old_price if old_price else 100.0
            )
            reasons: list[str] = []
            material = False
            if not new_pricing.get("pricing_possible", False):
                material = True
                reasons.append("Current pricing inputs are incomplete or invalid.")
            if price_change_pct > 0.5:
                material = True
                reasons.append(f"Unit price changed by {price_change_pct:.2f}%.")

            fresh_fs = fresh_feasibility.get("feasibility") or {}
            feasibility_status = str(fresh_fs.get("overall_status") or "").lower()
            if "cannot" in feasibility_status:
                material = True
                reasons.append("Stock, capacity, or delivery commitment changed.")

            draft = _draft(state.get("final_draft_json"))
            if draft and draft.valid_until:
                valid_until = None
                for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                    try:
                        valid_until = datetime.strptime(str(draft.valid_until), fmt).date()
                        break
                    except ValueError:
                        continue
                if valid_until and valid_until < datetime.now(timezone.utc).date():
                    material = True
                    reasons.append("Quotation validity has expired.")

            customer = (state.get("customer_360") or {}).get("customer") or {}
            credit_limit = float(customer.get("credit_limit") or 0)
            outstanding = float(customer.get("outstanding_amount") or 0)
            po_total = float(
                (state.get("po_extraction") or {}).get("total_amount_inc_gst") or 0
            )
            if credit_limit and outstanding + po_total > credit_limit:
                reasons.append("PO would exceed the customer's current credit limit.")
                return {
                    **fresh_feasibility,
                    **fresh_pricing,
                    "order_revalidation": {
                        "outcome": "minor_change",
                        "price_change_pct": round(price_change_pct, 4),
                        "reasons": reasons,
                    },
                    "needs_human_approval": True,
                    "human_approval_stage": "po_revalidation",
                    "human_approval_reasons": reasons,
                    "stages_completed": ["po_revalidation_requires_approval"],
                }

            outcome = "material_change" if material else "unchanged"
            result = {
                **fresh_feasibility,
                **fresh_pricing,
                "order_revalidation": {
                    "outcome": outcome,
                    "price_change_pct": round(price_change_pct, 4),
                    "reasons": reasons,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
                "needs_human_approval": False,
                "human_approval_stage": None,
                "business_milestone": BusinessMilestone.PO_VALIDATED.value,
                "stages_completed": ["commercial_and_operational_state_revalidated"],
            }
            if material:
                result.update({
                    "outbound_message": (
                        "We cannot confirm this purchase order against the earlier "
                        "quotation because current conditions changed: " + "; ".join(reasons)
                    ),
                    "pipeline_status": PipelineStatus.AWAITING_CORRECTED_PO.value,
                    "waiting_for": WaitingFor.CORRECTED_PO.value,
                })
            return result
        except Exception as exc:
            return failure_result("revalidate_order_state", exc)

    async def reserve_inventory(state: CompletePipelineState) -> dict:
        """Atomically reserve current inventory before marking the order won."""
        from app.database.models import InventoryRecord, InventoryReservation

        requirement = state.get("requirement") or {}
        product = requirement.get("matched_product") or {}
        product_code = product.get("product_code")
        quantity = float((state.get("po_extraction") or {}).get("quantity") or 0)
        if not product_code or quantity <= 0:
            return failure_result(
                "reserve_inventory",
                ValueError("Resolved product code and positive PO quantity are required."),
            )
        async with session_factory() as session:
            existing = await session.scalar(
                select(InventoryReservation).where(
                    InventoryReservation.business_id == state["business_id"],
                    InventoryReservation.po_id == state.get("po_id"),
                    InventoryReservation.status == "reserved",
                )
            )
            if existing:
                return {"stages_completed": ["inventory_already_reserved"]}
            rows = (await session.scalars(
                select(InventoryRecord).where(
                    InventoryRecord.business_id == state["business_id"],
                    InventoryRecord.product_code == product_code,
                    InventoryRecord.is_active.is_(True),
                ).with_for_update()
            )).all()
            reserved_by_row = dict((await session.execute(
                select(
                    InventoryReservation.inventory_record_id,
                    func.coalesce(func.sum(InventoryReservation.quantity), 0),
                ).where(
                    InventoryReservation.business_id == state["business_id"],
                    InventoryReservation.product_code == product_code,
                    InventoryReservation.status == "reserved",
                ).group_by(InventoryReservation.inventory_record_id)
            )).all())
            available = sum(
                max(0.0, float(row.available_qty) - float(reserved_by_row.get(row.id, 0)))
                for row in rows
            )
            if available < quantity:
                return failure_result(
                    "reserve_inventory",
                    ValueError(
                        f"Insufficient current stock for {product_code}: "
                        f"required {quantity}, available {available}."
                    ),
                )
            remaining = quantity
            for row in rows:
                row_available = max(
                    0.0,
                    float(row.available_qty) - float(reserved_by_row.get(row.id, 0)),
                )
                allocation = min(row_available, remaining)
                if allocation <= 0:
                    continue
                session.add(InventoryReservation(
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    po_id=state["po_id"],
                    inventory_record_id=row.id,
                    product_code=product_code,
                    quantity=allocation,
                ))
                remaining -= allocation
                if remaining <= 0:
                    break
            await session.commit()
        return {"stages_completed": ["inventory_reserved"]}

    async def send_po_correction(state: CompletePipelineState) -> dict:
        try:
            msg = state.get("outbound_message","")
            await _send_outbound(
                state,
                msg,
                "po_correction",
                subject="Purchase order correction required",
            )
            return {
                "pipeline_status":   PipelineStatus.AWAITING_CORRECTED_PO.value,
                "waiting_for":       WaitingFor.CORRECTED_PO.value,
                "stages_completed":  ["po_correction_sent"],
            }
        except Exception as e:
            return {"error": f"send_po_correction: {e}", "stages_completed": []}

    async def mark_order_won(state: CompletePipelineState) -> dict:
        try:
            async with session_factory() as session:
                from sqlalchemy import update
                await session.execute(
                    update(ia.Lead)
                    .where(ia.Lead.inquiry_id == state.get("inquiry_id",""))
                    .values(status=ia.LeadStatus.WON.value)
                )
                await ia.log_action(session,"lead",state.get("inquiry_id",""),
                                    "order_won","po_agent",
                                    {"quotation_number":state.get("quotation_number"),
                                     "po_id":state.get("po_id")})
                from app.followups.service import (
                    cancel_open_followup_jobs,
                )
                await cancel_open_followup_jobs(
                    session,
                    business_id=state["business_id"],
                    thread_id=state["thread_id"],
                    reason="Order won.",
                )
                await session.commit()
            await _record_transition(
                state,
                "order.won",
                "po_agent",
                {"po_id": state.get("po_id")},
                "lead",
                state.get("lead_id"),
            )
            return {
                "order_won":        True,
                "pipeline_status":  PipelineStatus.PROCESSING.value,
                "business_milestone": BusinessMilestone.ORDER_WON.value,
                "waiting_for": WaitingFor.NONE.value,
                "stages_completed": ["order_marked_won"],
            }
        except Exception as e:
            return {"error": f"mark_order_won: {e}", "stages_completed": []}

    async def create_sales_order(state: CompletePipelineState) -> dict:
        try:
            ext_dict = state.get("po_extraction") or {}
            ext = poe.POExtraction(**ext_dict) if ext_dict else None
            async with session_factory() as session:
                so = poe.SalesOrder(
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    thread_id=state["thread_id"],
                    inquiry_id=state.get("inquiry_id",""),
                    quotation_id=state.get("quotation_id",""),
                    po_id=state.get("po_id",""),
                    po_number=ext.po_number if ext else "",
                    buyer_company=ext.buyer_company if ext else "",
                    quantity=ext.quantity if ext else None,
                    unit=ext.unit if ext else "MT",
                    total_value=ext.total_amount_inc_gst if ext else None,
                    delivery_date=ext.delivery_date if ext else None,
                    delivery_location=ext.delivery_location if ext else None,
                    payment_terms=ext.payment_terms if ext else None,
                    status="confirmed",
                )
                session.add(so)
                await session.flush()
                await ia.log_action(session,"sales_order",so.id,
                                    "sales_order_created","po_agent",
                                    {"po_number":so.po_number,"total":so.total_value})
                await session.commit()
            return {
                "sales_order_id":   so.id,
                "pipeline_status":  PipelineStatus.PROCESSING.value,
                "business_milestone": BusinessMilestone.SALES_ORDER_CREATED.value,
                "stages_completed": ["sales_order_created"],
            }
        except Exception as e:
            return {"error": f"create_sales_order: {e}", "stages_completed": []}

    # ══════════════════════════════════════════════════════════════════
    # SUB-PIPELINE E: HANDOFF  (runs after mark_order_won)
    # ══════════════════════════════════════════════════════════════════

    async def build_handoff_packages(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            po_ext  = poe.POExtraction(**(state.get("po_extraction") or {}))
            fs      = _feasibility(state.get("feasibility"))
            pr      = _pricing(state.get("pricing"))
            ql      = _qualification(state.get("qualification"))

            summary = hb.build_all_packages(
                sales_order_id=state.get("sales_order_id",""),
                po=po_ext, feasibility=fs, pricing=pr,
                qualification=ql,
                quotation_number=state.get("quotation_number",""),
                rag_context=rag_context,
            )
            return {
                "handoff_id":          summary.handoff_id,
                "handoff_summary_json": summary.model_dump_json(),
                "pipeline_status":     PipelineStatus.PROCESSING.value,
                "stages_completed":    [f"handoff_built_{len(summary.packages)}_depts"],
            }
        except Exception as e:
            return {"error": f"build_handoff_packages: {e}", "stages_completed": []}

    async def dispatch_handoff(state: CompletePipelineState) -> dict:
        try:
            summary_data = json.loads(state.get("handoff_summary_json") or "{}")
            summary      = hb.HandoffSummary(**summary_data)
            async with session_factory() as session:
                records = await hd.dispatch_all(
                    session,
                    summary,
                    client,
                    business_id=state["business_id"],
                    customer_id=state.get("customer_id"),
                    thread_id=state["thread_id"],
                    outbound_dispatcher=outbound_dispatcher,
                )
            return {
                "departments_notified": [r.department for r in records
                                         if r.status == hd.HandoffRecordStatus.SENT],
                "pipeline_status":      PipelineStatus.PROCESSING.value,
                "stages_completed":     ["handoff_dispatched"],
            }
        except Exception as e:
            return {"error": f"dispatch_handoff: {e}", "stages_completed": []}

    async def finalize_audit(state: CompletePipelineState) -> dict:
        try:
            async with session_factory() as session:
                await ia.log_action(session,"pipeline",state.get("inquiry_id",""),
                                    "pipeline_complete","pipeline",
                                    {"final_status":    state.get("pipeline_status"),
                                     "stages_done":     state.get("stages_completed",[]),
                                     "departments":     state.get("departments_notified",[]),
                                     "order_won":       state.get("order_won",False),
                                     "quotation_number":state.get("quotation_number"),
                                     "handoff_id":      state.get("handoff_id"),
                                     "completed_at":    datetime.utcnow().isoformat()})
                await session.commit()
            await _record_transition(
                state,
                "handoff.completed",
                "handoff_agent",
                {
                    "sales_order_id": state.get("sales_order_id"),
                    "departments": state.get("departments_notified", []),
                },
                "handoff",
                state.get("handoff_id"),
            )
            return {
                "pipeline_status":  PipelineStatus.HANDED_OFF.value,
                "business_milestone": BusinessMilestone.HANDED_OFF.value,
                "waiting_for": WaitingFor.NONE.value,
                "stages_completed": ["pipeline_finalized"],
            }
        except Exception as e:
            return {"error": f"finalize_audit: {e}", "stages_completed": []}

    # ══════════════════════════════════════════════════════════════════
    # SHARED: HUMAN APPROVAL GATE
    # ══════════════════════════════════════════════════════════════════

    async def request_approval(state: CompletePipelineState) -> dict:
        try:
            stage   = state.get("human_approval_stage","unknown")
            reasons = state.get("human_approval_reasons",[])
            async with session_factory() as session:
                await ia.log_action(session,"pipeline",state.get("inquiry_id",""),
                                    "human_approval_requested","pipeline",
                                    {"stage":stage,"reasons":reasons})
                await session.commit()
            await _record_transition(
                state,
                "approval.requested",
                "sales_graph",
                {"stage": stage, "reasons": reasons},
            )
            return {
                "pipeline_status":  PipelineStatus.AWAITING_APPROVAL.value,
                "waiting_for": (
                    WaitingFor.FINANCE_MANAGER.value
                    if stage in {"qualification", "pricing", "negotiation", "po_revalidation"}
                    else WaitingFor.PRODUCTION_MANAGER.value
                    if stage in {"requirement", "feasibility"}
                    else WaitingFor.SALES_MANAGER.value
                ),
                "status_reason": "; ".join(reasons) if reasons else f"Approval required for {stage}.",
                "stages_completed": [f"approval_requested:{stage}"],
            }
        except Exception as e:
            return {"error": f"request_approval: {e}", "stages_completed": []}

    nodes = {
        # Entry
        "check_trigger":              check_trigger,
        # Inquiry → Quotation
        "extract_inquiry":            extract_inquiry,
        "send_inquiry_followup":      send_inquiry_followup,
        "normalize_requirement":      normalize_requirement,
        "exact_product_code_lookup":  exact_product_code_lookup,
        "structured_specification_match": structured_specification_match,
        "match_requirement": with_rag_context(
            agent_name="requirement_agent",
            rag_adapter=rag_adapter,
            handler=match_requirement,
        ),
        "resolve_customer_identity": resolve_customer_identity,
        "load_customer_360": load_customer_360,
        "qualify_customer": with_rag_context(
            agent_name="qualification_agent",
            rag_adapter=rag_adapter,
            handler=qualify_customer,
        ),
        "check_feasibility": with_rag_context(
            agent_name="feasibility_agent",
            rag_adapter=rag_adapter,
            handler=check_feasibility,
            top_k=15,
        ),
        "compute_pricing": with_rag_context(
            agent_name="pricing_agent",
            rag_adapter=rag_adapter,
            handler=compute_pricing,
            top_k=30,
        ),
        "generate_quotation": with_rag_context(
            agent_name="quotation_agent",
            rag_adapter=rag_adapter,
            handler=generate_quotation,
        ),
        "dispatch_quotation":         dispatch_quotation,
        # Follow-up
        "compose_reminder": with_rag_context(
            agent_name="followup_agent",
            rag_adapter=rag_adapter,
            handler=compose_reminder,
        ),
        "dispatch_followup":          dispatch_followup,
        # Customer reply → Negotiation
        "analyze_reply":              analyze_reply,
        "record_commercial_acceptance": record_commercial_acceptance,
        "compose_positive_response":  compose_positive_response,
        "evaluate_counteroffer": with_rag_context(
            agent_name="negotiation_agent",
            rag_adapter=rag_adapter,
            handler=evaluate_counteroffer,
        ),
        "prepare_revised_quotation":  prepare_revised_quotation,
        "compose_rejection":          compose_rejection,
        "compose_objection_response": with_rag_context(
            agent_name="negotiation_agent",
            rag_adapter=rag_adapter,
            handler=compose_objection_response,
        ),
        "dispatch_message":           dispatch_message,
        # PO Handling
        "extract_po_fields":          extract_po_fields,
        "validate_po": with_rag_context(
            agent_name="purchase_order_agent",
            rag_adapter=rag_adapter,
            handler=validate_po,
        ),
        "send_po_correction":         send_po_correction,
        "revalidate_order_state": with_rag_context(
            agent_name="purchase_order_agent",
            rag_adapter=rag_adapter,
            handler=revalidate_order_state,
        ),
        "reserve_inventory":          reserve_inventory,
        "mark_order_won":             mark_order_won,
        "create_sales_order":         create_sales_order,
        # Handoff
        "build_handoff_packages": with_rag_context(
            agent_name="handoff_agent",
            rag_adapter=rag_adapter,
            handler=build_handoff_packages,
        ),
        "dispatch_handoff":           dispatch_handoff,
        "finalize_audit":             finalize_audit,
        # Shared
        "request_approval":           request_approval,
    }
    return {
        name: _contract_node(name, handler)
        for name, handler in nodes.items()
    }


# ═══════════════════════════════════════════════════════════════════════════
# ROUTING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

# ── Entry router ──────────────────────────────────────────────────────────

def route_from_trigger(state: CompletePipelineState) -> str:
    if state.get("error"):
        return END
    t = state.get("trigger","inquiry")
    if t == "inquiry":        return "extract_inquiry"
    if t == "followup":       return "compose_reminder"
    if t == "customer_reply": return "analyze_reply"
    if t == "po_received":    return "extract_po_fields"
    if t == "retry_pricing":  return "compute_pricing"
    if t == "approved":
        stage = state.get("approved_stage","")
        resume = {
            "requirement": "resolve_customer_identity",
            "qualification": "check_feasibility",
            "feasibility":   "compute_pricing",
            "pricing":       "generate_quotation",
            "negotiation":   "prepare_revised_quotation",
            "po":            "revalidate_order_state",
            "po_revalidation": "reserve_inventory",
        }
        return resume.get(stage, END)
    return END

# ── Inquiry sub-pipeline ──────────────────────────────────────────────────

def route_after_extraction(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    return "send_inquiry_followup" if state.get("needs_followup") else "normalize_requirement"


def route_after_normalize_requirement(state: CompletePipelineState) -> str:
    return END if state.get("error") else "exact_product_code_lookup"


def route_after_exact_product_lookup(state: CompletePipelineState) -> str:
    if state.get("error"):
        return END
    return (
        "structured_specification_match"
        if state.get("product_candidate")
        else "match_requirement"
    )


def route_after_structured_product_match(state: CompletePipelineState) -> str:
    if state.get("error"):
        return END
    if state.get("product_resolution_status") == "technical_review":
        return "request_approval"
    if state.get("product_resolution_status") == "exact":
        return "resolve_customer_identity"
    return "match_requirement"


def route_after_identity_resolution(state: CompletePipelineState) -> str:
    return END if state.get("error") else "load_customer_360"


def route_after_requirement_match(state: CompletePipelineState) -> str:
    if state.get("error"):
        return END
    if state.get("needs_human_approval") and state.get("human_approval_stage") == "requirement":
        return "request_approval"
    return "resolve_customer_identity"


def route_after_customer_360(state: CompletePipelineState) -> str:
    return END if state.get("error") else "qualify_customer"

def route_after_qualification(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    if state.get("needs_human_approval") and state.get("human_approval_stage") == "qualification":
        return "request_approval"
    return "check_feasibility"

def route_after_feasibility(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    if state.get("needs_human_approval") and state.get("human_approval_stage") == "feasibility":
        return "request_approval"
    return "compute_pricing"

def route_after_pricing(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    pricing = state.get("pricing") or {}
    if pricing and not pricing.get("pricing_possible", False):
        return END
    if state.get("needs_human_approval") and state.get("human_approval_stage") == "pricing":
        return "request_approval"
    return "generate_quotation"

# ── Customer reply sub-pipeline ───────────────────────────────────────────

def route_after_analyze_reply(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    rt = state.get("reply_type","unknown")
    if rt == "counter_offer": return "evaluate_counteroffer"
    if rt == "objection":     return "compose_objection_response"
    if rt == "po_text":       return "extract_po_fields"
    if rt == "commercial_acceptance": return "record_commercial_acceptance"
    if rt == "positive":      return "compose_positive_response"
    return END

def route_after_counteroffer(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    na = state.get("negotiation_analysis") or {}
    decision = na.get("decision","")
    if decision == "acceptable":     return "prepare_revised_quotation"
    if decision == "needs_approval": return "request_approval"
    if decision == "below_floor":    return "compose_rejection"
    return END

def route_after_negotiation_compose(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    return "dispatch_message"

# ── PO handling sub-pipeline ──────────────────────────────────────────────

def route_after_validate_po(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    verdict = state.get("po_verdict","")
    val_dict = state.get("po_validation") or {}
    if val_dict.get("requires_customer_correction"): return "send_po_correction"
    if state.get("needs_human_approval"):            return "request_approval"
    if verdict in ("valid","minor_mismatch"):        return "revalidate_order_state"
    return "send_po_correction"


def route_after_order_revalidation(state: CompletePipelineState) -> str:
    if state.get("error"):
        return END
    outcome = (state.get("order_revalidation") or {}).get("outcome")
    if state.get("needs_human_approval"):
        return "request_approval"
    if outcome == "unchanged":
        return "reserve_inventory"
    if outcome == "material_change":
        return "send_po_correction"
    return END


def route_after_inventory_reservation(state: CompletePipelineState) -> str:
    return END if state.get("error") else "mark_order_won"

def route_after_mark_won(state: CompletePipelineState) -> str:
    return END if state.get("error") else "create_sales_order"

def route_after_create_so(state: CompletePipelineState) -> str:
    return END if state.get("error") else "build_handoff_packages"

def route_after_build_handoff(state: CompletePipelineState) -> str:
    return END if state.get("error") else "dispatch_handoff"

def route_after_dispatch_handoff(state: CompletePipelineState) -> str:
    return END if state.get("error") else "finalize_audit"


# ═══════════════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_complete_graph(
    session_factory,
    rag_adapter,
    client,
    checkpointer=None,
    outbound_dispatcher=None,
    structured_data=None,
    sales_context_service=None,
):
    nodes = build_all_nodes(
        session_factory,
        rag_adapter,
        client,
        outbound_dispatcher=outbound_dispatcher,
        structured_data=structured_data,
        sales_context_service=sales_context_service,
    )
    g     = StateGraph(CompletePipelineState)

    for name, fn in nodes.items():
        g.add_node(name, fn)

    # ── Entry ─────────────────────────────────────────────────────────
    g.add_edge(START, "check_trigger")
    g.add_conditional_edges("check_trigger", route_from_trigger, {
        "extract_inquiry":     "extract_inquiry",
        "compose_reminder":    "compose_reminder",
        "analyze_reply":       "analyze_reply",
        "extract_po_fields":   "extract_po_fields",
        "check_feasibility":   "check_feasibility",    # approved:qualification
        "compute_pricing":     "compute_pricing",       # approved:feasibility
        "generate_quotation":  "generate_quotation",    # approved:pricing
        "prepare_revised_quotation": "prepare_revised_quotation",  # approved:negotiation
        "resolve_customer_identity": "resolve_customer_identity",
        "revalidate_order_state": "revalidate_order_state",
        "reserve_inventory":   "reserve_inventory",
        END: END,
    })

    # ── Sub-pipeline A: Inquiry → Quotation ───────────────────────────
    g.add_conditional_edges("extract_inquiry", route_after_extraction, {
        "send_inquiry_followup": "send_inquiry_followup",
        "normalize_requirement": "normalize_requirement",
        END: END,
    })
    g.add_edge("send_inquiry_followup", END)
    g.add_conditional_edges("normalize_requirement", route_after_normalize_requirement, {
        "exact_product_code_lookup": "exact_product_code_lookup", END: END,
    })
    g.add_conditional_edges("exact_product_code_lookup", route_after_exact_product_lookup, {
        "structured_specification_match": "structured_specification_match",
        "match_requirement": "match_requirement", END: END,
    })
    g.add_conditional_edges("structured_specification_match", route_after_structured_product_match, {
        "request_approval": "request_approval",
        "resolve_customer_identity": "resolve_customer_identity",
        "match_requirement": "match_requirement", END: END,
    })
    g.add_conditional_edges("match_requirement", route_after_requirement_match, {
        "request_approval": "request_approval",
        "resolve_customer_identity": "resolve_customer_identity",
        END: END,
    })
    g.add_conditional_edges(
        "resolve_customer_identity",
        route_after_identity_resolution,
        {"load_customer_360": "load_customer_360", END: END},
    )
    g.add_conditional_edges(
        "load_customer_360",
        route_after_customer_360,
        {"qualify_customer": "qualify_customer", END: END},
    )
    g.add_conditional_edges("qualify_customer", route_after_qualification, {
        "request_approval":  "request_approval",
        "check_feasibility": "check_feasibility",
        END: END,
    })
    g.add_conditional_edges("check_feasibility", route_after_feasibility, {
        "request_approval": "request_approval",
        "compute_pricing":  "compute_pricing",
        END: END,
    })
    g.add_conditional_edges("compute_pricing", route_after_pricing, {
        "request_approval":   "request_approval",
        "generate_quotation": "generate_quotation",
        END: END,
    })
    g.add_edge("generate_quotation", "dispatch_quotation")
    g.add_edge("dispatch_quotation", END)

    # ── Sub-pipeline B: Follow-up ─────────────────────────────────────
    g.add_edge("compose_reminder",  "dispatch_followup")
    g.add_edge("dispatch_followup", END)

    # ── Sub-pipeline C: Customer Reply → Negotiation ──────────────────
    g.add_conditional_edges("analyze_reply", route_after_analyze_reply, {
        "evaluate_counteroffer":      "evaluate_counteroffer",
        "compose_objection_response": "compose_objection_response",
        "extract_po_fields":          "extract_po_fields",
        "record_commercial_acceptance": "record_commercial_acceptance",
        "compose_positive_response":  "compose_positive_response",
        "dispatch_message":           "dispatch_message",
        END: END,
    })
    g.add_edge("record_commercial_acceptance", "dispatch_message")
    g.add_edge("compose_positive_response", "dispatch_message")
    g.add_conditional_edges("evaluate_counteroffer", route_after_counteroffer, {
        "prepare_revised_quotation": "prepare_revised_quotation",
        "request_approval":          "request_approval",
        "compose_rejection":         "compose_rejection",
        END: END,
    })
    g.add_conditional_edges("prepare_revised_quotation", route_after_negotiation_compose, {
        "dispatch_message": "dispatch_message", END: END,
    })
    g.add_conditional_edges("compose_rejection", route_after_negotiation_compose, {
        "dispatch_message": "dispatch_message", END: END,
    })
    g.add_conditional_edges("compose_objection_response", route_after_negotiation_compose, {
        "dispatch_message": "dispatch_message", END: END,
    })
    g.add_edge("dispatch_message", END)

    # ── Sub-pipeline D: PO Handling ───────────────────────────────────
    g.add_edge("extract_po_fields", "validate_po")
    g.add_conditional_edges("validate_po", route_after_validate_po, {
        "send_po_correction": "send_po_correction",
        "request_approval":   "request_approval",
        "revalidate_order_state": "revalidate_order_state",
        END: END,
    })
    g.add_edge("send_po_correction", END)
    g.add_conditional_edges("revalidate_order_state", route_after_order_revalidation, {
        "request_approval": "request_approval",
        "reserve_inventory": "reserve_inventory",
        "send_po_correction": "send_po_correction",
        END: END,
    })
    g.add_conditional_edges("reserve_inventory", route_after_inventory_reservation, {
        "mark_order_won": "mark_order_won", END: END,
    })
    g.add_conditional_edges("mark_order_won", route_after_mark_won, {
        "create_sales_order": "create_sales_order", END: END,
    })

    # ── Sub-pipeline E: Handoff ───────────────────────────────────────
    g.add_conditional_edges("create_sales_order", route_after_create_so, {
        "build_handoff_packages": "build_handoff_packages", END: END,
    })
    g.add_conditional_edges("build_handoff_packages", route_after_build_handoff, {
        "dispatch_handoff": "dispatch_handoff", END: END,
    })
    g.add_conditional_edges("dispatch_handoff", route_after_dispatch_handoff, {
        "finalize_audit": "finalize_audit", END: END,
    })
    g.add_edge("finalize_audit", END)

    # ── Shared: approval gate always ends pipeline ────────────────────
    g.add_edge("request_approval", END)

    return g.compile(checkpointer=checkpointer)


# ═══════════════════════════════════════════════════════════════════════════
# DEFAULT INITIAL STATE FACTORY
# ═══════════════════════════════════════════════════════════════════════════

def make_initial_state(
    trigger: str,
    business_id: str,
    source: str = "email",
    raw_text: str = "",
    sender_identifier: str | None = None,
    **overrides,
) -> dict:
    """
    Returns a fully-initialised CompletePipelineState dict.
    Pass overrides for any field that already has a value
    (e.g. when re-invoking with trigger='po_received').
    """
    base = {
        "business_id":        business_id,
        "thread_id":          "",
        "customer_id":        None,
        "customer_resolution": None,
        "customer_match_review_id": None,
        "customer_360": None,
        "sales_context": None,
        "trigger":            trigger,
        "approved_stage":     None,
        "pipeline_status":    PipelineStatus.PROCESSING.value,
        "business_milestone": None,
        "waiting_for":        WaitingFor.NONE.value,
        "status_reason":      None,
        "current_node":       None,
        "status_updated_at":  None,
        "failure":            None,
        "source":             source,
        "raw_text":           raw_text,
        "sender_identifier":  sender_identifier,
        "stages_completed":   [],
        "human_approval_reasons": [],
        "rag_audit":           [],
        "inquiry_id":         None,
        "lead_id":            None,
        "quotation_id":       None,
        "quotation_number":   None,
        "sales_order_id":     None,
        "po_id":              None,
        "handoff_id":         None,
        "extraction":         None,
        "needs_followup":     False,
        "requirement":        None,
        "normalized_requirement": None,
        "product_candidate":  None,
        "product_resolution_status": None,
        "customer_profile":   None,
        "qualification":      None,
        "feasibility":        None,
        "pricing":            None,
        "final_draft_json":   None,
        "quotation_sent_at":  None,
        "quotation_html":     None,
        "quotation_message":  None,
        "quotation_delivery_id": None,
        "followup_attempt":   1,
        "followup_tone":      None,
        "followup_message":   None,
        "followup_job_id":    None,
        "followup_record_id": None,
        "followup_provider_message_id": None,
        "customer_reply_text":None,
        "reply_type":         None,
        "objection":          None,
        "negotiation_analysis":None,
        "revised_draft_json": None,
        "negotiation_version":0,
        "po_raw_text":        None,
        "po_extraction":      None,
        "po_validation":      None,
        "po_verdict":         None,
        "order_revalidation": None,
        "order_won":          False,
        "handoff_summary_json":None,
        "departments_notified":[],
        "needs_human_approval":False,
        "human_approval_stage":None,
        "outbound_channel":   "email",
        "outbound_recipient": sender_identifier or "",
        "outbound_message":   None,
        "error":              None,
    }
    base.update(overrides)
    return base


# ═══════════════════════════════════════════════════════════════════════════
# DEMO
# ═══════════════════════════════════════════════════════════════════════════

# async def _demo():
#     from database import init_db, create_all_tables
#     from document_store import DocumentManager

#     init_db()
#     await create_all_tables()
#     from database import _engine as engine
#     Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

#     client_obj = None
#     if os.environ.get("GEMINI_API_KEY"):
#         from google import genai
#         client_obj = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

#     dm  = DocumentManager()
#     app = build_complete_graph(Session, dm, client_obj)

#     print(f"Graph nodes ({len(app.get_graph().nodes)}): {list(app.get_graph().nodes.keys())}")

#     # ── Scenario 1: New inquiry ────────────────────────────────────────
#     print("\n" + "="*62)
#     print("TRIGGER: inquiry")
#     s1 = make_initial_state(
#         trigger="inquiry", source="email",
#         raw_text="Need 500 MT MS Billet IS2062, 100x100mm, deliver Ludhiana by 30 June. Ramesh Kumar, Apex Steel Pvt Ltd",
#         sender_identifier="ramesh@apexsteel.in",
#     )
#     r1 = await app.ainvoke(s1)
#     print(f"Status    : {r1.get('pipeline_status')}")
#     print(f"Stages    : {r1.get('stages_completed')}")
#     print(f"Quotation : {r1.get('quotation_number','—')}")
#     if r1.get("error"): print(f"Error     : {r1['error']}")

#     # ── Scenario 2: Follow-up reminder (day 7) ────────────────────────
#     print("\n" + "="*62)
#     print("TRIGGER: followup  (Celery scheduler, day 7)")
#     s2 = make_initial_state(
#         trigger="followup", source="email",
#         sender_identifier="ramesh@apexsteel.in",
#         followup_attempt=2,         # day 7 = attempt 2
#         quotation_number=r1.get("quotation_number","QT-DEMO"),
#         final_draft_json=r1.get("final_draft_json"),
#         inquiry_id=r1.get("inquiry_id"),
#         outbound_channel="email",
#         outbound_recipient="ramesh@apexsteel.in",
#     )
#     r2 = await app.ainvoke(s2)
#     print(f"Status : {r2.get('pipeline_status')}")
#     print(f"Stages : {r2.get('stages_completed')}")

#     # ── Scenario 3: Customer sends counter-offer ──────────────────────
#     print("\n" + "="*62)
#     print("TRIGGER: customer_reply  (counter-offer)")
#     s3 = make_initial_state(
#         trigger="customer_reply", source="email",
#         raw_text="",
#         sender_identifier="ramesh@apexsteel.in",
#         customer_reply_text="Your price is high. Can you do ₹14,000/MT?",
#         inquiry_id=r1.get("inquiry_id"),
#         quotation_number=r1.get("quotation_number"),
#         final_draft_json=r1.get("final_draft_json"),
#         pricing=r1.get("pricing"),
#         outbound_channel="email",
#         outbound_recipient="ramesh@apexsteel.in",
#     )
#     r3 = await app.ainvoke(s3)
#     print(f"Status    : {r3.get('pipeline_status')}")
#     print(f"Reply type: {r3.get('reply_type')}")
#     print(f"Decision  : {(r3.get('negotiation_analysis') or {}).get('decision','—')}")
#     print(f"Stages    : {r3.get('stages_completed')}")

#     # ── Scenario 4: Customer sends PO ─────────────────────────────────
#     print("\n" + "="*62)
#     print("TRIGGER: po_received")
#     sample_po = """
#     PO Number: APX-2025-0891 | Date: 20-06-2025
#     Buyer: Apex Steel Pvt Ltd | GSTIN: 03AABCA1234C1Z5
#     Ship To: Apex Works, Sahnewal, Ludhiana
#     Product: MS Billet IS2062 100x100mm | Qty: 500 MT
#     Rate: ₹14,200/MT (ex-GST) | GST: 18% | Total: ₹8,378,000
#     Payment: 20% advance, 80% net 45 days | Delivery: 30-06-2025
#     """
#     s4 = make_initial_state(
#         trigger="po_received", source="email",
#         raw_text="",
#         sender_identifier="ramesh@apexsteel.in",
#         po_raw_text=sample_po,
#         inquiry_id=r1.get("inquiry_id"),
#         quotation_id=r1.get("quotation_id"),
#         quotation_number=r1.get("quotation_number"),
#         final_draft_json=r1.get("final_draft_json"),
#         pricing=r1.get("pricing"),
#         feasibility=r1.get("feasibility"),
#         qualification=r1.get("qualification"),
#         outbound_channel="email",
#         outbound_recipient="ramesh@apexsteel.in",
#     )
#     r4 = await app.ainvoke(s4)
#     print(f"Status         : {r4.get('pipeline_status')}")
#     print(f"Order won      : {r4.get('order_won')}")
#     print(f"Sales order ID : {r4.get('sales_order_id','—')}")
#     print(f"Handoff ID     : {r4.get('handoff_id','—')}")
#     print(f"Depts notified : {r4.get('departments_notified')}")
#     print(f"Stages         : {r4.get('stages_completed')}")
#     if r4.get("error"): print(f"Error          : {r4['error']}")


# Build and invoke this graph from FastAPI lifespan, where the database
# session factory, Gemini client, and LangGraphRAGAdapter are initialized.
