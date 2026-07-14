"""
langgraph_pipeline.py

Wires every agent we have built into a compiled LangGraph StateGraph.

Nodes (one per agent stage):
  extract_inquiry      →  normalize text, run Gemini extraction, save Lead to DB
  send_followup        →  compose follow-up message when fields are missing (terminal)
  match_requirement    →  ChromaDB catalog search, gap analysis, RequirementSummary
  qualify_customer     →  DB lookup + score → QualificationResult (hot/warm/cold)
  check_feasibility    →  inventory + production capacity + delivery → FeasibilityResult
  compute_pricing      →  cost build-up + discount rules + approval flag → PricingResult
  generate_quotation   →  QuotationDraft + HTML render + saved to DB
  request_approval     →  logs HumanApprovalRequest, pauses pipeline (terminal)

Graph edges:
  START ──────────────────────────────► extract_inquiry
  extract_inquiry ──[missing fields?]──► send_followup ──► END
                  ──[all fields]───────► match_requirement
  match_requirement ──────────────────► qualify_customer
  qualify_customer ──[credit risk?]───► request_approval ──► END
                   ──[ok]─────────────► check_feasibility
  check_feasibility ──[review needed?]► request_approval ──► END
                    ──[ok]────────────► compute_pricing
  compute_pricing ──[approval needed?]► request_approval ──► END
                  ──[ok]──────────────► generate_quotation ──► END

Run:
    python langgraph_pipeline.py
"""

import sys
import os
import asyncio
import operator
import json
from typing import TypedDict, Annotated, Optional
from importlib import import_module

from langgraph.graph import StateGraph, START, END
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

sys.path.insert(0, os.path.dirname(__file__))

# ── Lazy module loader (handles 03_xxx filenames) ─────────────────────────

def _mods() -> dict:
    return {
        "ia":   import_module("inquiry_agent"),
        "req":  import_module("04_requirement_matching"),
        "lk":   import_module("05_customer_lookup"),
        "qual": import_module("06_customer_qualification"),
        "inv":  import_module("07_inventory_check"),
        "fe":   import_module("08_feasibility_engine"),
        "pd":   import_module("09_pricing_documents"),
        "pe":   import_module("10_pricing_engine"),
        "qb":   import_module("11_quotation_builder"),
        "qr":   import_module("12_quotation_renderer"),
        "cat":  import_module("03_catalog_ingestion"),
    }


# ═══════════════════════════════════════════════════════════════════════════
# PIPELINE STATE
# Flows through every node. Each node returns ONLY the keys it changes.
# Annotated[list, operator.add] means lists are appended, not replaced.
# ═══════════════════════════════════════════════════════════════════════════

class PipelineState(TypedDict):

    # ── Input (set once at START) ──────────────────────────────────────
    source: str                       # "email" | "whatsapp" | "web_form"
    raw_text: str
    sender_identifier: Optional[str]

    # ── Accumulating lists (each node appends, never overwrites) ───────
    stages_completed:       Annotated[list[str], operator.add]
    human_approval_reasons: Annotated[list[str], operator.add]

    # ── Identifiers ────────────────────────────────────────────────────
    inquiry_id:      Optional[str]
    lead_id:         Optional[str]
    quotation_number: Optional[str]

    # ── Stage outputs stored as plain dicts (JSON-serializable) ────────
    # Each downstream node deserializes from these dicts.
    extraction:       Optional[dict]   # InquiryExtraction.model_dump()
    requirement:      Optional[dict]   # RequirementSummary.model_dump()
    customer_profile: Optional[dict]   # CustomerProfile.model_dump()
    qualification:    Optional[dict]   # QualificationResult.model_dump()
    feasibility:      Optional[dict]   # FeasibilityResult.model_dump()
    pricing:          Optional[dict]   # PricingResult.model_dump()

    # ── Control flags ──────────────────────────────────────────────────
    needs_followup:       bool
    needs_human_approval: bool
    human_approval_stage: Optional[str]   # "qualification" | "feasibility" | "pricing"

    # ── Error ──────────────────────────────────────────────────────────
    error: Optional[str]


# ═══════════════════════════════════════════════════════════════════════════
# NODE FACTORY
# Returns all node functions with session_factory, dm, client injected
# via closure — nodes never take these as arguments.
# ═══════════════════════════════════════════════════════════════════════════

def build_nodes(session_factory, dm, client):
    """
    dm     : DocumentManager instance (holds parsed CSVs + ChromaDB)
    client : google.genai.Client | None  (None = no API key, fallbacks used)
    """
    M = _mods()
    ia   = M["ia"]
    req  = M["req"]
    lk   = M["lk"]
    qual = M["qual"]
    inv  = M["inv"]
    fe   = M["fe"]
    pe   = M["pe"]
    qb   = M["qb"]
    qr   = M["qr"]

    # ------------------------------------------------------------------
    # NODE 1 — extract_inquiry
    # Normalises raw text, calls Gemini structured extraction, saves Lead.
    # ------------------------------------------------------------------
    async def extract_inquiry(state: PipelineState) -> dict:
        try:
            raw = ia.normalize_inquiry(
                ia.InquirySource(state["source"]),
                state["raw_text"],
                state.get("sender_identifier"),
            )

            if client:
                extraction = ia.extract_inquiry(raw, client)
            else:
                # Graceful degradation — no API key
                extraction = ia.InquiryExtraction(
                    inquiry_id=raw.inquiry_id,
                    extraction_confidence=0.0,
                    missing_fields=list(ia.REQUIRED_FIELDS),
                )

            async with session_factory() as session:
                lead = await ia.create_lead(session, raw, extraction)

            return {
                "inquiry_id":    raw.inquiry_id,
                "lead_id":       lead.id,
                "extraction":    extraction.model_dump(),
                "needs_followup": bool(extraction.missing_fields),
                "stages_completed": ["inquiry"],
            }
        except Exception as e:
            return {"error": f"extract_inquiry: {e}", "stages_completed": []}

    # ------------------------------------------------------------------
    # NODE 2 — send_followup
    # Composes a channel-specific follow-up message and logs it.
    # Terminal node — pipeline pauses until customer replies.
    # ------------------------------------------------------------------
    async def send_followup(state: PipelineState) -> dict:
        try:
            ext_dict = state.get("extraction") or {}
            extraction = ia.InquiryExtraction(**ext_dict)
            raw = ia.normalize_inquiry(
                ia.InquirySource(state["source"]),
                state["raw_text"],
                state.get("sender_identifier"),
            )
            followup = ia.compose_followup_message(extraction, raw, client)
            async with session_factory() as session:
                await ia.log_action(
                    session, "lead", state.get("lead_id", ""),
                    "followup_sent", "inquiry_agent",
                    {"channel": state["source"],
                     "missing": extraction.missing_fields,
                     "message": followup.message_text if followup else ""},
                )
                await session.commit()
            return {"stages_completed": ["followup_sent"]}
        except Exception as e:
            return {"error": f"send_followup: {e}", "stages_completed": []}

    # ------------------------------------------------------------------
    # NODE 3 — match_requirement
    # Embeds the product request, queries ChromaDB, runs gap analysis.
    # ------------------------------------------------------------------
    async def match_requirement(state: PipelineState) -> dict:
        try:
            ext_dict = state.get("extraction") or {}
            extraction = ia.InquiryExtraction(**ext_dict)

            collection = await dm.get_catalog_collection() if hasattr(dm, "get_catalog_collection") \
                         else None

            if collection and client:
                requirement = req.match_requirement(extraction, collection, client)
            else:
                # Fallback: no ChromaDB / no client → CUSTOM match
                requirement = req.RequirementSummary(
                    inquiry_id=extraction.inquiry_id,
                    match_type=req.MatchType.CUSTOM,
                    similarity_score=0.0,
                    summary_text="Catalog match skipped — no collection or client available.",
                    requires_human_review=True,
                    human_review_reason="Catalog embedding unavailable.",
                )

            return {
                "requirement":      requirement.model_dump(),
                "stages_completed": ["requirement"],
            }
        except Exception as e:
            return {"error": f"match_requirement: {e}", "stages_completed": []}

    # ------------------------------------------------------------------
    # NODE 4 — qualify_customer
    # Looks up customer in DB, scores, classifies hot/warm/cold.
    # ------------------------------------------------------------------
    async def qualify_customer(state: PipelineState) -> dict:
        try:
            ext_dict = state.get("extraction") or {}
            extraction = ia.InquiryExtraction(**ext_dict)

            async with session_factory() as session:
                profile = await lk.lookup_customer(session, extraction)

            result = qual.qualify_lead(extraction.inquiry_id, profile, client)

            return {
                "customer_profile": profile.model_dump(),
                "qualification":    result.model_dump(),
                "needs_human_approval": result.credit_risk_flag,
                "human_approval_stage": "qualification" if result.credit_risk_flag else None,
                "human_approval_reasons": (
                    [result.credit_risk_reason] if result.credit_risk_flag and result.credit_risk_reason else []
                ),
                "stages_completed": ["qualification"],
            }
        except Exception as e:
            return {"error": f"qualify_customer: {e}", "stages_completed": []}

    # ------------------------------------------------------------------
    # NODE 5 — check_feasibility
    # Checks inventory + production capacity + delivery timeline.
    # ------------------------------------------------------------------
    async def check_feasibility(state: PipelineState) -> dict:
        try:
            ext_dict  = state.get("extraction")  or {}
            req_dict  = state.get("requirement")  or {}
            qual_dict = state.get("qualification") or {}

            extraction    = ia.InquiryExtraction(**ext_dict)
            requirement   = _rebuild_requirement(req_dict, req)
            qualification = qual.QualificationResult(**qual_dict)

            inventory_index = dm.get_inventory_index() if hasattr(dm, "get_inventory_index") \
                              else inv.parse_inventory_csv(inv.SAMPLE_INVENTORY_CSV)
            capacity_index  = dm.get_capacity_index() if hasattr(dm, "get_capacity_index") \
                              else fe.parse_capacity_csv(fe.SAMPLE_CAPACITY_CSV)
            delivery_index  = dm.get_delivery_index() if hasattr(dm, "get_delivery_index") \
                              else fe.parse_delivery_csv(fe.SAMPLE_DELIVERY_CSV)

            inv_result  = inv.check_inventory(requirement, extraction, inventory_index)
            feasibility = fe.check_feasibility(
                extraction, requirement, qualification,
                inv_result, capacity_index, delivery_index, client,
            )

            return {
                "feasibility":          feasibility.model_dump(),
                "needs_human_approval": feasibility.requires_human_review,
                "human_approval_stage": "feasibility" if feasibility.requires_human_review else None,
                "human_approval_reasons": feasibility.human_review_reasons,
                "stages_completed": ["feasibility"],
            }
        except Exception as e:
            return {"error": f"check_feasibility: {e}", "stages_completed": []}

    # ------------------------------------------------------------------
    # NODE 6 — compute_pricing
    # Cost build-up, discount policy, approval flag.
    # ------------------------------------------------------------------
    async def compute_pricing(state: PipelineState) -> dict:
        try:
            ext_dict  = state.get("extraction")   or {}
            req_dict  = state.get("requirement")   or {}
            qual_dict = state.get("qualification") or {}
            feas_dict = state.get("feasibility")   or {}

            extraction    = ia.InquiryExtraction(**ext_dict)
            requirement   = _rebuild_requirement(req_dict, req)
            qualification = qual.QualificationResult(**qual_dict)
            feasibility   = fe.FeasibilityResult(**feas_dict)

            pricing_docs = dm.get_pricing_docs() if hasattr(dm, "get_pricing_docs") \
                           else import_module("09_pricing_documents").load_pricing_documents()

            pricing = pe.compute_pricing(
                extraction, requirement, qualification, feasibility, pricing_docs, client
            )

            return {
                "pricing":              pricing.model_dump(),
                "needs_human_approval": pricing.requires_human_approval,
                "human_approval_stage": "pricing" if pricing.requires_human_approval else None,
                "human_approval_reasons": pricing.approval_reasons,
                "stages_completed": ["pricing"],
            }
        except Exception as e:
            return {"error": f"compute_pricing: {e}", "stages_completed": []}

    # ------------------------------------------------------------------
    # NODE 7 — generate_quotation
    # Build QuotationDraft, render HTML, persist to DB.
    # ------------------------------------------------------------------
    async def generate_quotation(state: PipelineState) -> dict:
        try:
            ext_dict  = state.get("extraction")       or {}
            req_dict  = state.get("requirement")       or {}
            qual_dict = state.get("qualification")     or {}
            feas_dict = state.get("feasibility")       or {}
            pr_dict   = state.get("pricing")           or {}
            cp_dict   = state.get("customer_profile")  or {}

            extraction    = ia.InquiryExtraction(**ext_dict)
            qualification = qual.QualificationResult(**qual_dict)
            feasibility   = fe.FeasibilityResult(**feas_dict)
            pricing       = pe.PricingResult(**pr_dict)
            customer      = lk.CustomerProfile(**cp_dict)

            draft = qb.build_quotation(
                extraction, pricing, feasibility, qualification, customer
            )
            html = qr.render_quotation_html(draft)

            async with session_factory() as session:
                record = await qr.save_quotation(session, draft, html)

            return {
                "quotation_number": draft.quotation_number,
                "stages_completed": ["quotation"],
            }
        except Exception as e:
            return {"error": f"generate_quotation: {e}", "stages_completed": []}

    # ------------------------------------------------------------------
    # NODE 8 — request_approval
    # Logs HumanApprovalRequest. Pipeline pauses here — resumes via
    # POST /approve/{id} in prod which re-invokes the graph.
    # ------------------------------------------------------------------
    async def request_approval(state: PipelineState) -> dict:
        try:
            stage   = state.get("human_approval_stage", "unknown")
            reasons = state.get("human_approval_reasons", [])
            async with session_factory() as session:
                await ia.log_action(
                    session, "pipeline", state.get("inquiry_id", ""),
                    "human_approval_requested", "pipeline",
                    {"stage": stage, "reasons": reasons},
                )
                await session.commit()
            return {"stages_completed": [f"approval_requested:{stage}"]}
        except Exception as e:
            return {"error": f"request_approval: {e}", "stages_completed": []}

    return {
        "extract_inquiry":    extract_inquiry,
        "send_followup":      send_followup,
        "match_requirement":  match_requirement,
        "qualify_customer":   qualify_customer,
        "check_feasibility":  check_feasibility,
        "compute_pricing":    compute_pricing,
        "generate_quotation": generate_quotation,
        "request_approval":   request_approval,
    }


# ═══════════════════════════════════════════════════════════════════════════
# ROUTING FUNCTIONS (conditional edges)
# Each returns the name of the next node.
# ═══════════════════════════════════════════════════════════════════════════

def route_after_extraction(state: PipelineState) -> str:
    if state.get("error"):
        return END
    if state.get("needs_followup"):
        return "send_followup"
    return "match_requirement"


def route_after_qualification(state: PipelineState) -> str:
    if state.get("error"):
        return END
    if state.get("needs_human_approval") and state.get("human_approval_stage") == "qualification":
        return "request_approval"
    return "check_feasibility"


def route_after_feasibility(state: PipelineState) -> str:
    if state.get("error"):
        return END
    if state.get("needs_human_approval") and state.get("human_approval_stage") == "feasibility":
        return "request_approval"
    return "compute_pricing"


def route_after_pricing(state: PipelineState) -> str:
    if state.get("error"):
        return END
    if state.get("needs_human_approval") and state.get("human_approval_stage") == "pricing":
        return "request_approval"
    return "generate_quotation"


# ═══════════════════════════════════════════════════════════════════════════
# GRAPH BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_graph(session_factory, dm, client):
    """
    Returns a compiled LangGraph app ready to invoke.

    Usage:
        app = build_graph(session_factory, dm, client)
        result = await app.ainvoke(initial_state)
    """
    nodes = build_nodes(session_factory, dm, client)

    graph = StateGraph(PipelineState)

    # ── Add all nodes ──────────────────────────────────────────────────
    for name, fn in nodes.items():
        graph.add_node(name, fn)

    # ── Fixed edges ────────────────────────────────────────────────────
    graph.add_edge(START,               "extract_inquiry")
    graph.add_edge("send_followup",     END)
    graph.add_edge("match_requirement", "qualify_customer")
    graph.add_edge("request_approval",  END)
    graph.add_edge("generate_quotation", END)

    # ── Conditional edges ──────────────────────────────────────────────
    graph.add_conditional_edges(
        "extract_inquiry",
        route_after_extraction,
        {
            "send_followup":     "send_followup",
            "match_requirement": "match_requirement",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "qualify_customer",
        route_after_qualification,
        {
            "request_approval":  "request_approval",
            "check_feasibility": "check_feasibility",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "check_feasibility",
        route_after_feasibility,
        {
            "request_approval": "request_approval",
            "compute_pricing":  "compute_pricing",
            END: END,
        },
    )
    graph.add_conditional_edges(
        "compute_pricing",
        route_after_pricing,
        {
            "request_approval":   "request_approval",
            "generate_quotation": "generate_quotation",
            END: END,
        },
    )

    return graph.compile()


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _rebuild_requirement(req_dict: dict, req_mod):
    """
    Reconstructs RequirementSummary from its dict.
    Handles nested CatalogProduct if present.
    """
    if not req_dict:
        return req_mod.RequirementSummary(
            inquiry_id="", match_type=req_mod.MatchType.CUSTOM,
            similarity_score=0.0, summary_text="",
        )
    cat_mod = import_module("03_catalog_ingestion")
    matched_dict = req_dict.get("matched_product")
    if matched_dict:
        req_dict = {**req_dict, "matched_product": cat_mod.CatalogProduct(**matched_dict)}
    # gap_analysis nested object
    gap_dict = req_dict.get("gap_analysis")
    if gap_dict:
        req_dict = {**req_dict, "gap_analysis": req_mod.GapAnalysis(**gap_dict)}
    return req_mod.RequirementSummary(**req_dict)


def _print_state(state: PipelineState):
    print(f"\n{'─'*60}")
    print(f"  Inquiry ID     : {state.get('inquiry_id')}")
    print(f"  Lead ID        : {state.get('lead_id')}")
    print(f"  Stages done    : {state.get('stages_completed', [])}")
    print(f"  Needs followup : {state.get('needs_followup')}")
    print(f"  Needs approval : {state.get('needs_human_approval')}")
    if state.get("human_approval_stage"):
        print(f"  Approval stage : {state['human_approval_stage']}")
        print(f"  Reasons        : {state.get('human_approval_reasons', [])}")
    if state.get("quotation_number"):
        print(f"  Quotation      : {state['quotation_number']}")
        pr = state.get("pricing") or {}
        print(f"  Invoice total  : ₹{pr.get('total_invoice_value', 0):,.2f}")
    if state.get("error"):
        print(f"  ERROR          : {state['error']}")
    print(f"{'─'*60}")


# ═══════════════════════════════════════════════════════════════════════════
# DEMO — runs three scenarios through the compiled graph
# ═══════════════════════════════════════════════════════════════════════════

# async def _demo():
#     from document_store import DocumentManager
#     from database import init_db, create_all_tables, _engine

#     init_db()
#     await create_all_tables()
#     from database import _engine as engine
#     Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

#     client_obj = None
#     if os.environ.get("GEMINI_API_KEY"):
#         from google import genai
#         client_obj = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

#     dm  = DocumentManager()
#     app = build_graph(Session, dm, client_obj)

#     scenarios = [
#         {
#             "label": "Complete inquiry — Apex Steel, 500 MT MS Billet",
#             "state": {
#                 "source": "email",
#                 "raw_text": (
#                     "Hi, need 500 MT MS Billet IS2062, 100x100mm, "
#                     "delivery Ludhiana, within 30 days. "
#                     "Ramesh Kumar, Apex Steel Pvt Ltd"
#                 ),
#                 "sender_identifier": "ramesh@apexsteel.in",
#                 "stages_completed": [],
#                 "human_approval_reasons": [],
#                 "needs_followup": False,
#                 "needs_human_approval": False,
#                 "human_approval_stage": None,
#                 "inquiry_id": None, "lead_id": None, "quotation_number": None,
#                 "extraction": None, "requirement": None, "customer_profile": None,
#                 "qualification": None, "feasibility": None, "pricing": None,
#                 "error": None,
#             },
#         },
#         {
#             "label": "Incomplete inquiry — missing name and company",
#             "state": {
#                 "source": "whatsapp",
#                 "raw_text": "Need 200 MT MS Pipe 2 inch ASAP",
#                 "sender_identifier": "+919812345678",
#                 "stages_completed": [],
#                 "human_approval_reasons": [],
#                 "needs_followup": False,
#                 "needs_human_approval": False,
#                 "human_approval_stage": None,
#                 "inquiry_id": None, "lead_id": None, "quotation_number": None,
#                 "extraction": None, "requirement": None, "customer_profile": None,
#                 "qualification": None, "feasibility": None, "pricing": None,
#                 "error": None,
#             },
#         },
#     ]

#     for scenario in scenarios:
#         print(f"\n{'='*60}")
#         print(f"SCENARIO: {scenario['label']}")
#         result = await app.ainvoke(scenario["state"])
#         _print_state(result)


if __name__ == "__main__":
    asyncio.run(build_graph())
    
    
    
# Fast API integration (commented out for demo purposes)
# app = build_graph(Session, document_manager, gemini_client)

# @router.post("/inquiry")
# async def handle_inquiry(body: InquiryRequest):
#     result = await app.ainvoke(initial_state_from(body))
#     return result