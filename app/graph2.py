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
from datetime import datetime
from typing import TypedDict, Annotated, Optional
from importlib import import_module
from pathlib import Path
from langgraph.graph import StateGraph, START, END
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.rag.langgraph_adapter import LangGraphRAGAdapter
from app.rag.models import AgentRAGContext

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

    # ── STAGE 7: Follow-up ─────────────────────────────────────────────
    followup_attempt:      int            # increments each reminder (1→4)
    followup_tone:         Optional[str]  # "gentle" | "moderate" | "urgent" | "final"
    followup_message:      Optional[str]  # composed message text

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
):
    async def wrapped(state: CompletePipelineState) -> dict:
        try:
            rag_context = await rag_adapter.get_context(
                agent_name=agent_name,
                state=state,
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

def build_all_nodes(session_factory, rag_adapter, client):
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

    def _draft(js): return qb.QuotationDraft(**json.loads(js)) if js else None
    def _pricing(d): return pe.PricingResult(**d) if d else None
    def _feasibility(d): return fe.FeasibilityResult(**d) if d else None
    def _qualification(d): return qual.QualificationResult(**d) if d else None

    # ══════════════════════════════════════════════════════════════════
    # ENTRY ROUTER — decides which sub-pipeline runs this invocation
    # ══════════════════════════════════════════════════════════════════

    async def check_trigger(state: CompletePipelineState) -> dict:
        """
        Does nothing except exist so the graph has a real START node.
        Routing happens in route_from_trigger() below.
        """
        return {"stages_completed": [f"trigger:{state['trigger']}"]}

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
                lead = await ia.create_lead(session, raw, extraction)

            return {
                "inquiry_id":       raw.inquiry_id,
                "lead_id":          lead.id,
                "extraction":       extraction.model_dump(),
                "needs_followup":   bool(extraction.missing_fields),
                "pipeline_status":  "inquiry_captured",
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
                _mock_send(state["source"], state.get("sender_identifier", ""), msg.message_text)
            async with session_factory() as session:
                await ia.log_action(session, "lead", state.get("lead_id",""),
                                    "missing_fields_followup_sent", "inquiry_agent",
                                    {"missing": ext.missing_fields})
                await session.commit()
            return {
                "outbound_message":  msg.message_text if msg else "",
                "pipeline_status":   "awaiting_customer_info",
                "stages_completed":  ["inquiry_followup_sent"],
            }
        except Exception as e:
            return {"error": f"send_inquiry_followup: {e}", "stages_completed": []}


# for product match 
    async def match_requirement(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            ext = ia.InquiryExtraction(
                **(state.get("extraction") or {})
            )
            result = req.match_requirement(
                extraction=ext,
                client=client,
                rag_context=rag_context,
            )
            return {
                "requirement": result.model_dump(),
                "stages_completed": ["requirement"],
            }
        except Exception as exc:
            return {
                "error": f"match_requirement: {exc}",
                "stages_completed": [],
            }

#for customer match         

    async def qualify_customer(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            ext = ia.InquiryExtraction(**(state.get("extraction") or {}))

            async with session_factory() as session:
                profile = await lk.lookup_customer(session, ext)

            result = qual.qualify_lead(
                ext.inquiry_id,
                profile,
                client,
                rag_context=rag_context,
            )

            return {
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

            result = fe.check_feasibility(
                extraction=ext,
                requirement=req_,
                qualification=ql,
                inventory=inv.parse_inventory_csv(inv.SAMPLE_INVENTORY_CSV),
                capacity_index=fe.parse_capacity_csv(fe.SAMPLE_CAPACITY_CSV),
                delivery_index=fe.parse_delivery_csv(fe.SAMPLE_DELIVERY_CSV),
                client=client,
                rag_context=rag_context,
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

            result = pe.compute_pricing(
                extraction=ext,
                requirement=req_,
                qualification=ql,
                feasibility=fs,
                docs=pd.load_pricing_documents(),
                client=client,
                rag_context=rag_context,
            )

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

            draft = qb.build_quotation(
                extraction=ext,
                pricing=pr,
                feasibility=fs,
                qualification=ql,
                customer=cust,
                rag_context=rag_context,
            )
            html = qr.render_quotation_html(draft)

            async with session_factory() as session:
                rec = await qr.save_quotation(session, draft, html)

            return {
                "quotation_id": rec.id,
                "quotation_number": draft.quotation_number,
                "final_draft_json": draft.model_dump_json(),
                "quotation_sent_at": datetime.utcnow().isoformat(),
                "pipeline_status": "quotation_sent",
                "stages_completed": ["quotation"],
            }
        except Exception as e:
            return {"error": f"generate_quotation: {e}", "stages_completed": []}

    # ══════════════════════════════════════════════════════════════════
    # SUB-PIPELINE B: FOLLOW-UP  (trigger="followup")
    # ══════════════════════════════════════════════════════════════════

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
            channel   = state.get("outbound_channel","email")
            recipient = state.get("outbound_recipient","")
            _mock_send(channel, recipient, msg)
            async with session_factory() as session:
                draft = _draft(state.get("final_draft_json"))
                schedule = ft.FOLLOW_UP_SCHEDULE[min(state.get("followup_attempt",1)-1,3)]
                await ft.create_followup_record(
                    session,
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
                "pipeline_status":    "followup_sent",
                "followup_attempt":   state.get("followup_attempt",1) + 1,
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
                "pipeline_status": "negotiating",
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
                    session, state.get("quotation_id",""),
                    state.get("quotation_number",""), revised, analysis,
                    "customer_counteroffer_accepted", "negotiation_agent",
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
                "pipeline_status":  "rejection_sent",
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
                "pipeline_status":   "objection_addressed",
                "stages_completed":  ["objection_response_composed"],
            }
        except Exception as e:
            return {"error": f"compose_objection_response: {e}", "stages_completed": []}

    async def dispatch_message(state: CompletePipelineState) -> dict:
        """Shared dispatch node — used by follow-up, negotiation, rejection, objection."""
        try:
            msg       = state.get("outbound_message","")
            channel   = state.get("outbound_channel","email")
            recipient = state.get("outbound_recipient","")
            _mock_send(channel, recipient, msg)
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
                    quotation_id=state.get("quotation_id"),
                    quotation_number=state.get("quotation_number"),
                    inquiry_id=state.get("inquiry_id"),
                )
            return {
                "po_id":          po_rec.id,
                "po_extraction":  ext.model_dump(),
                "pipeline_status":"po_extracted",
                "stages_completed":["po_extracted"],
            }
        except Exception as e:
            return {"error": f"extract_po_fields: {e}", "stages_completed": []}

    async def validate_po(
        state: CompletePipelineState,
        rag_context: AgentRAGContext,
    ) -> dict:
        try:
            draft = _draft(state.get("final_draft_json"))
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

    async def send_po_correction(state: CompletePipelineState) -> dict:
        try:
            msg = state.get("outbound_message","")
            _mock_send(state.get("outbound_channel","email"),
                       state.get("outbound_recipient",""), msg)
            return {
                "pipeline_status":   "awaiting_revised_po",
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
                await session.commit()
            return {
                "order_won":        True,
                "pipeline_status":  "won",
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
                "pipeline_status":  "sales_order_created",
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
                "pipeline_status":     "handoff_packages_built",
                "stages_completed":    [f"handoff_built_{len(summary.packages)}_depts"],
            }
        except Exception as e:
            return {"error": f"build_handoff_packages: {e}", "stages_completed": []}

    async def dispatch_handoff(state: CompletePipelineState) -> dict:
        try:
            summary_data = json.loads(state.get("handoff_summary_json") or "{}")
            summary      = hb.HandoffSummary(**summary_data)
            async with session_factory() as session:
                records = await hd.dispatch_all(session, summary, client)
            return {
                "departments_notified": [r.department for r in records
                                         if r.status == hd.HandoffRecordStatus.SENT],
                "pipeline_status":      "handoff_dispatched",
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
            return {
                "pipeline_status":  "handed_off",
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
            return {
                "pipeline_status":  f"awaiting_approval:{stage}",
                "stages_completed": [f"approval_requested:{stage}"],
            }
        except Exception as e:
            return {"error": f"request_approval: {e}", "stages_completed": []}

    return {
        # Entry
        "check_trigger":              check_trigger,
        # Inquiry → Quotation
        "extract_inquiry":            extract_inquiry,
        "send_inquiry_followup":      send_inquiry_followup,
        "match_requirement": with_rag_context(
            agent_name="requirement_agent",
            rag_adapter=rag_adapter,
            handler=match_requirement,
        ),
        "qualify_customer": with_rag_context(
            agent_name="qualification_agent",
            rag_adapter=rag_adapter,
            handler=qualify_customer,
        ),
        "check_feasibility": with_rag_context(
            agent_name="feasibility_agent",
            rag_adapter=rag_adapter,
            handler=check_feasibility,
        ),
        "compute_pricing": with_rag_context(
            agent_name="pricing_agent",
            rag_adapter=rag_adapter,
            handler=compute_pricing,
        ),
        "generate_quotation": with_rag_context(
            agent_name="quotation_agent",
            rag_adapter=rag_adapter,
            handler=generate_quotation,
        ),
        # Follow-up
        "compose_reminder": with_rag_context(
            agent_name="followup_agent",
            rag_adapter=rag_adapter,
            handler=compose_reminder,
        ),
        "dispatch_followup":          dispatch_followup,
        # Customer reply → Negotiation
        "analyze_reply":              analyze_reply,
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


# ═══════════════════════════════════════════════════════════════════════════
# ROUTING FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════

# ── Entry router ──────────────────────────────────────────────────────────

def route_from_trigger(state: CompletePipelineState) -> str:
    t = state.get("trigger","inquiry")
    if t == "inquiry":        return "extract_inquiry"
    if t == "followup":       return "compose_reminder"
    if t == "customer_reply": return "analyze_reply"
    if t == "po_received":    return "extract_po_fields"
    if t == "approved":
        stage = state.get("approved_stage","")
        resume = {
            "qualification": "check_feasibility",
            "feasibility":   "compute_pricing",
            "pricing":       "generate_quotation",
            "negotiation":   "prepare_revised_quotation",
            "po":            "mark_order_won",
        }
        return resume.get(stage, END)
    return END

# ── Inquiry sub-pipeline ──────────────────────────────────────────────────

def route_after_extraction(state: CompletePipelineState) -> str:
    if state.get("error"): return END
    return "send_inquiry_followup" if state.get("needs_followup") else "match_requirement"

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
    if rt == "positive":      return "dispatch_message"   # send "thanks, awaiting PO"
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
    if verdict in ("valid","minor_mismatch"):        return "mark_order_won"
    return "send_po_correction"

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

def build_complete_graph(session_factory, rag_adapter, client):
    nodes = build_all_nodes(session_factory, rag_adapter, client)
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
        "mark_order_won":      "mark_order_won",        # approved:po
        END: END,
    })

    # ── Sub-pipeline A: Inquiry → Quotation ───────────────────────────
    g.add_conditional_edges("extract_inquiry", route_after_extraction, {
        "send_inquiry_followup": "send_inquiry_followup",
        "match_requirement":     "match_requirement",
        END: END,
    })
    g.add_edge("send_inquiry_followup", END)
    g.add_edge("match_requirement",     "qualify_customer")
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
    g.add_edge("generate_quotation", END)   # pipeline pauses; re-invoked on next event

    # ── Sub-pipeline B: Follow-up ─────────────────────────────────────
    g.add_edge("compose_reminder",  "dispatch_followup")
    g.add_edge("dispatch_followup", END)

    # ── Sub-pipeline C: Customer Reply → Negotiation ──────────────────
    g.add_conditional_edges("analyze_reply", route_after_analyze_reply, {
        "evaluate_counteroffer":      "evaluate_counteroffer",
        "compose_objection_response": "compose_objection_response",
        "extract_po_fields":          "extract_po_fields",
        "dispatch_message":           "dispatch_message",
        END: END,
    })
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
        "mark_order_won":     "mark_order_won",
        END: END,
    })
    g.add_edge("send_po_correction", END)
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

    return g.compile()


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
        "trigger":            trigger,
        "approved_stage":     None,
        "pipeline_status":    "new",
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
        "customer_profile":   None,
        "qualification":      None,
        "feasibility":        None,
        "pricing":            None,
        "final_draft_json":   None,
        "quotation_sent_at":  None,
        "followup_attempt":   1,
        "followup_tone":      None,
        "followup_message":   None,
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
