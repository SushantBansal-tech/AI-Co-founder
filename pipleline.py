"""
pipeline.py — chains every agent into one callable.

Usage:
    from pipeline import run_pipeline
    result = await run_pipeline(session, dm, raw_inquiry)

What it does:
  1. Extract inquiry → save Lead
  2. Match requirement (uses catalog collection from dm)
  3. Qualify customer (looks up customer in DB)
  4. Check feasibility (uses inventory + capacity from dm)
  5. Compute pricing (uses pricing docs from dm)
  6. Build + render quotation
  7. On each stage: write result to Lead row + AuditLog
  8. On any human-approval flag: create HumanApprovalRequest + pause
  9. Returns PipelineResult with every stage's output

This file is what FastAPI calls. Agents don't call each other directly.
"""

import os
import json
from datetime import datetime
from typing import Optional
from importlib import import_module
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database import (
    Lead, LeadStatus, Customer, Quotation, HumanApprovalRequest,
    AgentRun, ApprovalStatus, log_action, init_db, create_all_tables,
)
from document_store import DocumentManager
from settings import GEMINI_API_KEY


# ── Lazy-load all agent modules ───────────────────────────────────────────

def _ia():   return import_module("inquiry_agent")
def _req():  return import_module("04_requirement_matching")
def _lk():   return import_module("05_customer_lookup")
def _qual(): return import_module("06_customer_qualification")
def _fe():   return import_module("08_feasibility_engine")
def _pe():   return import_module("10_pricing_engine")
def _qb():   return import_module("11_quotation_builder")
def _qr():   return import_module("12_quotation_renderer")


# ── Pipeline result ───────────────────────────────────────────────────────

@dataclass
class PipelineResult:
    inquiry_id: str
    lead_id: Optional[str] = None
    stages_completed: list[str] = field(default_factory=list)
    paused_at: Optional[str] = None        # stage name if waiting for human
    approval_request_id: Optional[str] = None
    error: Optional[str] = None
    completed: bool = False

    # Stage outputs (None = not reached yet)
    extraction: object = None
    requirement: object = None
    qualification: object = None
    feasibility: object = None
    pricing: object = None
    quotation_draft: object = None
    quotation_record_id: Optional[str] = None


# ── Helpers ───────────────────────────────────────────────────────────────

async def _get_or_create_run(session: AsyncSession, inquiry_id: str) -> AgentRun:
    result = await session.execute(
        select(AgentRun).where(AgentRun.inquiry_id == inquiry_id)
    )
    run = result.scalar_one_or_none()
    if run is None:
        run = AgentRun(inquiry_id=inquiry_id)
        session.add(run)
        await session.flush()
    return run


async def _pause_for_approval(
    session: AsyncSession,
    run: AgentRun,
    inquiry_id: str,
    stage: str,
    reasons: list[str],
    context: dict,
) -> str:
    """Creates a HumanApprovalRequest row and pauses the pipeline."""
    req = HumanApprovalRequest(
        inquiry_id=inquiry_id,
        stage=stage,
        reasons=reasons,
        context_json=json.dumps(context, default=str),
        status=ApprovalStatus.PENDING,
    )
    session.add(req)
    await session.flush()

    run.current_stage = f"awaiting_approval:{stage}"
    await log_action(session, "agent_run", run.id,
                     "pipeline_paused", "pipeline",
                     {"stage": stage, "reasons": reasons,
                      "approval_request_id": req.id})
    await session.commit()
    return req.id


async def _update_lead(session: AsyncSession, lead: Lead, **kwargs):
    for k, v in kwargs.items():
        setattr(lead, k, v)
    lead.updated_at = datetime.utcnow()


# ── Main pipeline entry ───────────────────────────────────────────────────

async def run_pipeline(
    session: AsyncSession,
    dm: DocumentManager,
    source: str,
    raw_text: str,
    sender_identifier: Optional[str] = None,
) -> PipelineResult:
    """
    Full inquiry → quotation pipeline.
    Call this from your FastAPI POST /inquiry endpoint.
    """
    ia   = _ia()
    genai_client = dm.get_gemini_client()

    result = PipelineResult(inquiry_id="")

    try:
        # ────────────────────────────────────────────────────────────────
        # STAGE 1: Inquiry extraction
        # ────────────────────────────────────────────────────────────────
        raw = ia.normalize_inquiry(
            ia.InquirySource(source), raw_text, sender_identifier
        )
        result.inquiry_id = raw.inquiry_id

        run = await _get_or_create_run(session, raw.inquiry_id)
        run.current_stage = "inquiry"

        extraction = ia.extract_inquiry(raw, genai_client) if genai_client else \
            ia.InquiryExtraction(inquiry_id=raw.inquiry_id, raw_text=raw.raw_text,
                                  extraction_confidence=0.0,
                                  missing_fields=list(ia.REQUIRED_FIELDS))

        # Persist lead
        lead_status = ia.LeadStatus.AWAITING_INFO if extraction.missing_fields else ia.LeadStatus.NEW
        lead = ia.Lead(
            inquiry_id=raw.inquiry_id, source=raw.source,
            sender_identifier=raw.sender_identifier,
            customer_name=extraction.customer_name,
            company_name=extraction.company_name,
            contact_person=extraction.contact_person,
            product_requested=extraction.product_requested,
            quantity=extraction.quantity,
            specifications=extraction.specifications,
            delivery_location=extraction.delivery_location,
            delivery_date=extraction.delivery_date,
            payment_expectation=extraction.payment_expectation,
            status=lead_status,
            missing_fields=extraction.missing_fields,
            raw_text=raw.raw_text,
        )
        session.add(lead)
        await session.flush()
        result.lead_id = lead.id

        await log_action(session, "lead", lead.id, "lead_created",
                         "inquiry_agent",
                         {"status": lead_status.value,
                          "missing": extraction.missing_fields})

        run.stages_completed.append("inquiry")
        result.stages_completed.append("inquiry")
        result.extraction = extraction

        # If critical fields missing — send follow-up, pause here
        if extraction.missing_fields:
            followup = ia.compose_followup_message(extraction, raw, genai_client)
            await log_action(session, "lead", lead.id, "followup_sent",
                             "inquiry_agent",
                             {"missing": extraction.missing_fields,
                              "message": followup.message_text if followup else ""})
            await session.commit()
            result.paused_at = "inquiry:awaiting_customer_reply"
            return result

        # ────────────────────────────────────────────────────────────────
        # STAGE 2: Requirement matching
        # ────────────────────────────────────────────────────────────────
        run.current_stage = "requirement"
        req_mod = _req()
        collection = await dm.get_catalog_collection()
        requirement = req_mod.match_requirement(extraction, collection, genai_client) \
            if collection and genai_client else None

        if requirement:
            await _update_lead(session, lead,
                requirement_summary_json=requirement.model_dump_json())
            run.stages_completed.append("requirement")
            result.stages_completed.append("requirement")
            result.requirement = requirement

        # ────────────────────────────────────────────────────────────────
        # STAGE 3: Customer qualification
        # ────────────────────────────────────────────────────────────────
        run.current_stage = "qualification"
        lk_mod   = _lk()
        qual_mod = _qual()

        customer_profile = await lk_mod.lookup_customer(session, extraction)
        qualification = qual_mod.qualify_lead(
            raw.inquiry_id, customer_profile, genai_client
        )

        await _update_lead(session, lead,
            qualification_result_json=qualification.model_dump_json())
        run.stages_completed.append("qualification")
        result.stages_completed.append("qualification")
        result.qualification = qualification

        # Credit risk → human approval
        if qualification.credit_risk_flag:
            approval_id = await _pause_for_approval(
                session, run, raw.inquiry_id, "qualification",
                [qualification.credit_risk_reason],
                {"qualification": qualification.model_dump()},
            )
            result.paused_at = "qualification:credit_risk"
            result.approval_request_id = approval_id
            return result

        # ────────────────────────────────────────────────────────────────
        # STAGE 4: Feasibility
        # ────────────────────────────────────────────────────────────────
        run.current_stage = "feasibility"
        inv_mod = import_module("07_inventory_check")
        fe_mod  = _fe()

        inv_result  = inv_mod.check_inventory(
            requirement, extraction, dm.get_inventory_index()
        ) if requirement else None
        feasibility = fe_mod.check_feasibility(
            extraction, requirement, qualification,
            inv_result, dm.get_capacity_index(),
            dm.get_delivery_index(), genai_client,
        ) if inv_result else None

        if feasibility:
            await _update_lead(session, lead,
                feasibility_result_json=feasibility.model_dump_json())
            run.stages_completed.append("feasibility")
            result.stages_completed.append("feasibility")
            result.feasibility = feasibility

        if feasibility and feasibility.requires_human_review:
            approval_id = await _pause_for_approval(
                session, run, raw.inquiry_id, "feasibility",
                feasibility.human_review_reasons,
                {"feasibility": feasibility.model_dump()},
            )
            result.paused_at = "feasibility:human_review"
            result.approval_request_id = approval_id
            return result

        # ────────────────────────────────────────────────────────────────
        # STAGE 5: Pricing
        # ────────────────────────────────────────────────────────────────
        run.current_stage = "pricing"
        pe_mod = _pe()
        pricing = pe_mod.compute_pricing(
            extraction, requirement, qualification,
            feasibility, dm.get_pricing_docs(), genai_client,
        ) if feasibility else None

        if pricing:
            await _update_lead(session, lead,
                pricing_result_json=pricing.model_dump_json())
            run.stages_completed.append("pricing")
            result.stages_completed.append("pricing")
            result.pricing = pricing

        if pricing and pricing.requires_human_approval:
            approval_id = await _pause_for_approval(
                session, run, raw.inquiry_id, "pricing",
                pricing.approval_reasons,
                {"pricing": pricing.model_dump()},
            )
            result.paused_at = "pricing:discount_approval"
            result.approval_request_id = approval_id
            return result

        # ────────────────────────────────────────────────────────────────
        # STAGE 6: Quotation generation
        # ────────────────────────────────────────────────────────────────
        run.current_stage = "quotation"
        qb_mod = _qb()
        qr_mod = _qr()

        draft = qb_mod.build_quotation(
            extraction, pricing, feasibility, qualification, customer_profile
        ) if pricing else None

        if draft:
            html = qr_mod.render_quotation_html(draft)
            q_record = await qr_mod.save_quotation(session, draft, html)
            run.stages_completed.append("quotation")
            result.stages_completed.append("quotation")
            result.quotation_draft   = draft
            result.quotation_record_id = q_record.id

        # ────────────────────────────────────────────────────────────────
        # Done
        # ────────────────────────────────────────────────────────────────
        run.completed    = True
        run.completed_at = datetime.utcnow()
        lead.status      = LeadStatus.IN_PROGRESS
        result.completed = True

        await session.commit()
        return result

    except Exception as e:
        result.error = str(e)
        await session.rollback()
        await log_action(session, "agent_run", result.inquiry_id or "unknown",
                         "pipeline_error", "pipeline", {"error": str(e)})
        await session.commit()
        return result


# ── Demo ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import asyncio
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

    async def main():
        init_db()
        await create_all_tables()

        from database import _engine
        Session = async_sessionmaker(_engine, class_=AsyncSession, expire_on_commit=False)
        dm = DocumentManager()

        print("Running full pipeline (no Gemini key — extraction skipped gracefully)...")
        async with Session() as session:
            result = await run_pipeline(
                session, dm,
                source="email",
                raw_text=(
                    "Hi, we need 500 MT of MS Billet IS2062, 100x100mm section, "
                    "delivery to Ludhiana, within 30 days. "
                    "Please quote. Regards, Ramesh Kumar, Apex Steel Pvt Ltd"
                ),
                sender_identifier="ramesh@apexsteel.in",
            )

        print(f"\nInquiry ID       : {result.inquiry_id}")
        print(f"Lead ID          : {result.lead_id}")
        print(f"Stages completed : {result.stages_completed}")
        print(f"Paused at        : {result.paused_at or 'N/A'}")
        print(f"Completed        : {result.completed}")
        if result.error:
            print(f"Error            : {result.error}")
        if result.quotation_draft:
            d = result.quotation_draft
            print(f"Quotation No     : {d.quotation_number}")
            print(f"Total invoice    : ₹{d.total_inc_gst:,.2f}")

    asyncio.run(main())