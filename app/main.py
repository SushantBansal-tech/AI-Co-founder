import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from importlib import import_module
from typing import Literal
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    Header,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from google import genai
#from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
from sqlalchemy import select
# from sqlalchemy.ext.asyncio import (
#     AsyncSession,
#     async_sessionmaker,
#     create_async_engine,
# )
from app.database import (
    Base,
    Customer,
    CustomerMatchReview,
    CustomerMatchReviewStatus,
    SessionFactory,
    dispose_database_engine,
    engine as database_engine,
)
from app.customers.merge_service import resolve_customer_match_review
from app.customers.customer_360 import get_customer_360
from app.events.interactions import record_interaction
from app.events.service import record_business_event
from app.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
    claim_request,
    complete_request,
    fail_request,
)
from app.documents.embeddings import LocalEmbeddingService
from app.documents.models import (
    DocumentType,
    DocumentUploadMetadata,
)
from app.documents.router import AgentDocumentRetriever
from app.documents.service import DocumentIngestionService
from app.documents.vector_store import DocumentVectorStore
from app.graph2 import (
    build_complete_graph,
    make_initial_state,
)
from app.rag.langgraph_adapter import LangGraphRAGAdapter
from app.rag.query_builder import canonical_agent_name
from app.rag.service import RAGContextService
from langgraph.checkpoint.postgres.aio import (
    AsyncPostgresSaver,
)


if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )


# Load GEMINI_API_KEY and other environment variables from .env.
load_dotenv()


# ---------------------------------------------------------------------------
# Application services
# Initialized during FastAPI lifespan.
# ---------------------------------------------------------------------------

embedding_service: LocalEmbeddingService | None = None
vector_store: DocumentVectorStore | None = None
ingestion_service: DocumentIngestionService | None = None
rag_service: RAGContextService | None = None
rag_adapter: LangGraphRAGAdapter | None = None
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///sales_os.db")

sales_graph = None
graph_checkpointer_context = None
graph_checkpointer = None
gemini_client = None


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RAGRetrieveRequest(BaseModel):
    business_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    state: dict = Field(default_factory=dict)
    top_k: int = Field(default=5, ge=1, le=20)


class StructuredDocumentRequest(BaseModel):
    business_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    document_name: str = Field(min_length=1)
    document_type: DocumentType


class InquiryRequest(BaseModel):
    business_id: str = Field(min_length=1)
    source: str = "email"
    raw_text: str = Field(min_length=1)
    sender_identifier: str | None = None


class PipelineEventRequest(BaseModel):
    business_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    trigger: Literal[
        "followup",
        "customer_reply",
        "po_received",
    ]
    customer_reply_text: str | None = None
    po_raw_text: str | None = None
    outbound_channel: str = "email"
    outbound_recipient: str | None = None


class ApprovalRequest(BaseModel):
    business_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    approved_stage: Literal[
        "qualification",
        "feasibility",
        "pricing",
        "negotiation",
        "po",
    ]


class CustomerMatchDecisionRequest(BaseModel):
    business_id: str = Field(min_length=1)
    action: Literal["merge", "keep_separate", "dismiss"]
    resolved_by: str = Field(min_length=1)
    notes: str | None = None


class CustomerNoteRequest(BaseModel):
    business_id: str = Field(min_length=1)
    content: str = Field(min_length=1)
    content_type: Literal[
        "call_summary",
        "meeting_note",
        "email_summary",
        "objection_summary",
        "relationship_note",
        "product_interest",
    ]
    thread_id: str | None = None
    interaction_id: str | None = None


# ---------------------------------------------------------------------------
# FastAPI lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedding_service
    global vector_store
    global ingestion_service
    global rag_service
    global rag_adapter
   # global database_engine
    global sales_graph
    global graph_checkpointer
    global gemini_client
    global graph_checkpointer_context
    # ------------------------------------------------------------------
    # 1. Initialize the local embedding model.
    # ------------------------------------------------------------------

    embedding_service = LocalEmbeddingService()

    # ------------------------------------------------------------------
    # 2. Initialize local persisted Qdrant.
    # ------------------------------------------------------------------

    vector_store = DocumentVectorStore(
        embedding_dimension=embedding_service.dimension,
        path="qdrant_data",
    )

    # ------------------------------------------------------------------
    # 3. Initialize document ingestion.
    # ------------------------------------------------------------------

    ingestion_service = DocumentIngestionService(
        embedding_service=embedding_service,
        vector_store=vector_store,
        upload_root="uploads",
    )

    # ------------------------------------------------------------------
    # 4. Initialize agent document retrieval and RAG services.
    # ------------------------------------------------------------------

    document_retriever = AgentDocumentRetriever(
        embedding_service=embedding_service,
        vector_store=vector_store,
    )

    rag_service = RAGContextService(
        retriever=document_retriever,
    )

    rag_adapter = LangGraphRAGAdapter(
        rag_service=rag_service,
    )

    # ------------------------------------------------------------------
    # 5. Initialize Gemini.
    # The graph can still start without a key, but LLM-dependent nodes
    # will use their configured fallbacks.
    # ------------------------------------------------------------------

    gemini_api_key = os.getenv("GEMINI_API_KEY")

    gemini_client = (
        genai.Client(api_key=gemini_api_key)
        if gemini_api_key
        else None
    )

    # ------------------------------------------------------------------
    # 6. Initialize the local SQLite database.
    # ------------------------------------------------------------------

    # database_engine = create_async_engine(
    #     "sqlite+aiosqlite:///sales_os.db",

        
    # )

    # Session = async_sessionmaker(
    #     database_engine,
    #     class_=AsyncSession,
    #     expire_on_commit=False,
    # )

    # ------------------------------------------------------------------
    # 7. Build the complete LangGraph.
    #
    # Building the graph imports the agent modules. Those modules register
    # their SQLAlchemy models on the shared Base metadata.
    # ------------------------------------------------------------------

    #graph_checkpointer = MemorySaver()
    langgraph_database_url = os.getenv(
    "LANGGRAPH_DATABASE_URL"
    )

    if not langgraph_database_url:
     raise RuntimeError(
        "LANGGRAPH_DATABASE_URL is required."
    )

    graph_checkpointer_context = (
       AsyncPostgresSaver.from_conn_string(
        langgraph_database_url
     )
    )

    graph_checkpointer = (
    await graph_checkpointer_context.__aenter__()
    )

    await graph_checkpointer.setup()


    sales_graph = build_complete_graph(
        session_factory=SessionFactory,
        rag_adapter=rag_adapter,
        client=gemini_client,
        checkpointer=graph_checkpointer,
    )

    # ------------------------------------------------------------------
    # 8. Create all registered database tables.
    # ------------------------------------------------------------------

    # inquiry_module = import_module("01_Inquiry")

    if DATABASE_URL.startswith("sqlite"):
     async with database_engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all
        )

    try:
        yield

    finally:
        # --------------------------------------------------------------
        # Clean shutdown.
        # --------------------------------------------------------------

        if vector_store is not None:
            vector_store.client.close()

        if graph_checkpointer_context is not None:
            await graph_checkpointer_context.__aexit__(
                None, None, None
            )

        if database_engine is not None:
            await dispose_database_engine()


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AI Sales Operations Agent",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "qdrant": (
            "ready"
            if vector_store is not None
            else "not_initialized"
        ),
        "rag": (
            "ready"
            if rag_adapter is not None
            else "not_initialized"
        ),
        "graph": (
            "ready"
            if sales_graph is not None
            else "not_initialized"
        ),
        "gemini": (
            "ready"
            if gemini_client is not None
            else "not_configured"
        ),
        "database": (
            "ready"
            if SessionFactory is not None
            else "not_initialized"
        ),
    }


# ---------------------------------------------------------------------------
# Document upload endpoint
# ---------------------------------------------------------------------------

@app.post("/documents/upload")
async def upload_document(
    business_id: str = Form(...),
    document_type: DocumentType = Form(...),
    allowed_agents_json: str = Form(...),
    version: str = Form("1.0"),
    file: UploadFile = File(...),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
):
    if ingestion_service is None:
        raise HTTPException(
            status_code=503,
            detail="Document ingestion service is not initialized.",
        )

    claim = None
    try:
        claim = await claim_request(
            business_id=business_id,
            endpoint="/documents/upload",
            idempotency_key=idempotency_key,
            payload={
                "business_id": business_id,
                "document_type": document_type.value,
                "allowed_agents_json": allowed_agents_json,
                "version": version,
                "filename": file.filename,
            },
        )
        if claim.is_cached:
            return claim.cached_response
        allowed_agents = json.loads(
            allowed_agents_json
        )

        if not isinstance(allowed_agents, list):
            raise ValueError(
                "allowed_agents_json must contain a JSON list"
            )

        if not allowed_agents:
            raise ValueError(
                "At least one allowed agent is required."
            )

        if not all(
            isinstance(agent_name, str)
            and agent_name.strip()
            for agent_name in allowed_agents
        ):
            raise ValueError(
                "allowed_agents_json entries must be "
                "non-empty strings"
            )

        # Accept both canonical names and shorter graph aliases.
        allowed_agents = list(
            dict.fromkeys(
                canonical_agent_name(
                    agent_name.strip()
                )
                for agent_name in allowed_agents
            )
        )

        metadata = DocumentUploadMetadata(
            business_id=business_id.strip(),
            document_type=document_type,
            allowed_agents=allowed_agents,
            version=version,
        )

        result = await ingestion_service.ingest(
            file=file,
            metadata=metadata,
        )

        response = jsonable_encoder(result)
        await complete_request(claim.event_id, response)
        return response

    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ValueError, json.JSONDecodeError) as exc:
        if claim and not claim.is_cached:
            await fail_request(claim.event_id, exc)
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Direct RAG retrieval test endpoint
# ---------------------------------------------------------------------------

@app.post("/rag/retrieve")
async def retrieve_agent_context(
    request: RAGRetrieveRequest,
):
    if rag_adapter is None:
        raise HTTPException(
            status_code=503,
            detail="RAG adapter is not initialized.",
        )

    state = {
        **request.state,
        "business_id": request.business_id,
    }

    try:
        context = await rag_adapter.get_context(
            agent_name=request.agent_name,
            state=state,
            top_k=request.top_k,
        )

        return {
            **context.model_dump(),
            "combined_text": context.combined_text,
            "chunk_ids": context.chunk_ids,
        }

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


@app.post("/rag/document")
async def retrieve_structured_document(
    request: StructuredDocumentRequest,
):
    if rag_adapter is None:
        raise HTTPException(
            status_code=503,
            detail="RAG adapter is not initialized.",
        )

    try:
        context = await rag_adapter.get_document_context(
            agent_name=request.agent_name,
            state={"business_id": request.business_id},
            document_name=request.document_name,
            document_type=request.document_type.value,
        )
        return context.model_dump()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc


# ---------------------------------------------------------------------------
# Inquiry processing endpoint
# ---------------------------------------------------------------------------

def _graph_config(thread_id: str) -> dict:
    return {
        "configurable": {
            "thread_id": thread_id,
        }
    }


async def _load_thread_state(thread_id: str) -> dict:
    if sales_graph is None:
        raise HTTPException(
            status_code=503,
            detail="Sales graph is not initialized.",
        )

    snapshot = await sales_graph.aget_state(
        _graph_config(thread_id)
    )
    state = dict(snapshot.values or {})

    if not state:
        raise HTTPException(
            status_code=404,
            detail=f"No pipeline state found for thread_id={thread_id}.",
        )

    return state


def _validate_thread_business(
    state: dict,
    business_id: str,
) -> None:
    if state.get("business_id") != business_id:
        raise HTTPException(
            status_code=403,
            detail="The thread does not belong to this business_id.",
        )


@app.post("/inquiries/process")
async def process_inquiry(
    request: InquiryRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
):
    if sales_graph is None:
        raise HTTPException(
            status_code=503,
            detail="Sales graph is not initialized.",
        )

    try:
        claim = await claim_request(
            business_id=request.business_id,
            endpoint="/inquiries/process",
            idempotency_key=idempotency_key,
            payload=request.model_dump(),
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claim.is_cached:
        return claim.cached_response

    initial_state = make_initial_state(
        trigger="inquiry",
        business_id=request.business_id,
        source=request.source,
        raw_text=request.raw_text,
        sender_identifier=request.sender_identifier,
    )

    thread_id = str(uuid4())
    config = _graph_config(thread_id)
    initial_state["thread_id"] = thread_id

    try:
        async with SessionFactory() as session:
            interaction = await record_interaction(
                session,
                business_id=request.business_id,
                thread_id=thread_id,
                direction="incoming",
                channel=request.source,
                message_type="inquiry",
                external_message_id=f"inquiry:{idempotency_key}",
                sender=request.sender_identifier,
                content=request.raw_text,
                status="received",
            )
            await record_business_event(
                session,
                business_id=request.business_id,
                thread_id=thread_id,
                event_type="inquiry.received",
                source="api",
                actor_type="customer",
                actor_id=request.sender_identifier,
                entity_type="interaction",
                entity_id=interaction.id,
                data={"channel": request.source},
            )
            await session.commit()

        result = await sales_graph.ainvoke(
            initial_state,
            config=config,
        )

        response = {
            "thread_id": thread_id,
            "state": result,
        }
        await complete_request(
            claim.event_id,
            jsonable_encoder(response),
            thread_id=thread_id,
            interaction_id=interaction.id,
        )
        return response

    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Sales graph execution failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Follow-up, customer reply, and PO event endpoint
# ---------------------------------------------------------------------------

@app.post("/pipeline/events")
async def process_pipeline_event(
    request: PipelineEventRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
):
    current_state = await _load_thread_state(
        request.thread_id
    )
    _validate_thread_business(
        current_state,
        request.business_id,
    )

    current_status = current_state.get("pipeline_status", "")
    if current_status.startswith("awaiting_approval:"):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Pipeline is {current_status}. "
                "Approve the pending stage before sending another event."
            ),
        )

    event_state = {
        "business_id": request.business_id,
        "trigger": request.trigger,
        "outbound_channel": request.outbound_channel,
        "outbound_recipient": (
            request.outbound_recipient
            or current_state.get("outbound_recipient")
            or current_state.get("sender_identifier")
            or ""
        ),
        "error": None,
    }

    if request.trigger == "customer_reply":
        if not request.customer_reply_text:
            raise HTTPException(
                status_code=400,
                detail=(
                    "customer_reply_text is required when "
                    "trigger='customer_reply'."
                ),
            )
        event_state["customer_reply_text"] = (
            request.customer_reply_text
        )

    if request.trigger == "po_received":
        if not request.po_raw_text:
            raise HTTPException(
                status_code=400,
                detail=(
                    "po_raw_text is required when "
                    "trigger='po_received'."
                ),
            )
        event_state["po_raw_text"] = request.po_raw_text

    try:
        claim = await claim_request(
            business_id=request.business_id,
            endpoint="/pipeline/events",
            idempotency_key=idempotency_key,
            payload=request.model_dump(),
            thread_id=request.thread_id,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claim.is_cached:
        return claim.cached_response

    try:
        incoming_content = (
            request.customer_reply_text
            or request.po_raw_text
            or f"Pipeline trigger: {request.trigger}"
        )
        async with SessionFactory() as session:
            interaction = await record_interaction(
                session,
                business_id=request.business_id,
                customer_id=current_state.get("customer_id"),
                lead_id=current_state.get("lead_id"),
                thread_id=request.thread_id,
                direction="incoming",
                channel=request.outbound_channel,
                message_type=request.trigger,
                external_message_id=(
                    f"pipeline_event:{idempotency_key}"
                ),
                sender=request.outbound_recipient,
                content=incoming_content,
                status="received",
            )
            await record_business_event(
                session,
                business_id=request.business_id,
                customer_id=current_state.get("customer_id"),
                lead_id=current_state.get("lead_id"),
                thread_id=request.thread_id,
                event_type=f"{request.trigger}.received",
                source="api",
                actor_type="customer",
                entity_type="interaction",
                entity_id=interaction.id,
            )
            await session.commit()
        result = await sales_graph.ainvoke(
            event_state,
            config=_graph_config(request.thread_id),
        )
        response = {
            "thread_id": request.thread_id,
            "state": result,
        }
        await complete_request(
            claim.event_id,
            jsonable_encoder(response),
            thread_id=request.thread_id,
            interaction_id=interaction.id,
        )
        return response
    except HTTPException:
        raise
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline event execution failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Human approval endpoint
# ---------------------------------------------------------------------------

@app.post("/pipeline/approve")
async def approve_pipeline_stage(
    request: ApprovalRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
):
    current_state = await _load_thread_state(
        request.thread_id
    )
    _validate_thread_business(
        current_state,
        request.business_id,
    )

    expected_status = (
        f"awaiting_approval:{request.approved_stage}"
    )
    current_status = current_state.get("pipeline_status")
    current_stage = current_state.get("human_approval_stage")

    if (
        current_status != expected_status
        or current_stage != request.approved_stage
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot approve stage '{request.approved_stage}'. "
                f"Current status is '{current_status}' and the pending "
                f"stage is '{current_stage}'."
            ),
        )

    if request.approved_stage == "pricing":
        pricing = current_state.get("pricing") or {}
        missing_inputs = (
            (pricing.get("price_logic") or {})
            .get("validation", {})
            .get("missing_inputs", [])
        )

        if (
            not pricing
            or not pricing.get("pricing_possible", False)
            or missing_inputs
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        "Pricing cannot be approved because fundamental "
                        "pricing inputs are missing or incompatible."
                    ),
                    "missing_inputs": missing_inputs,
                    "approval_reasons": pricing.get(
                        "approval_reasons",
                        [],
                    ),
                },
            )

    try:
        claim = await claim_request(
            business_id=request.business_id,
            endpoint="/pipeline/approve",
            idempotency_key=idempotency_key,
            payload=request.model_dump(),
            thread_id=request.thread_id,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claim.is_cached:
        return claim.cached_response

    approval_state = {
        "business_id": request.business_id,
        "trigger": "approved",
        "approved_stage": request.approved_stage,
        "needs_human_approval": False,
        "human_approval_stage": None,
        "error": None,
    }

    try:
        result = await sales_graph.ainvoke(
            approval_state,
            config=_graph_config(request.thread_id),
        )
        response = {
            "thread_id": request.thread_id,
            "approved_stage": request.approved_stage,
            "state": result,
        }
        async with SessionFactory() as session:
            await record_business_event(
                session,
                business_id=request.business_id,
                customer_id=current_state.get("customer_id"),
                lead_id=current_state.get("lead_id"),
                thread_id=request.thread_id,
                event_type="approval.granted",
                source="human",
                actor_type="employee",
                actor_id="api_user",
                data={"stage": request.approved_stage},
            )
            await session.commit()
        await complete_request(
            claim.event_id,
            jsonable_encoder(response),
            thread_id=request.thread_id,
        )
        return response
    except HTTPException:
        raise
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline approval failed: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Customer identity review and controlled deduplication
# ---------------------------------------------------------------------------

def _customer_review_payload(
    review: CustomerMatchReview,
    provisional: Customer | None,
    candidate: Customer | None,
) -> dict:
    return {
        "id": review.id,
        "business_id": review.business_id,
        "lead_id": review.lead_id,
        "confidence": review.confidence,
        "matched_signals": review.matched_signals,
        "conflicting_signals": review.conflicting_signals,
        "status": (
            review.status.value
            if hasattr(review.status, "value")
            else review.status
        ),
        "provisional_customer": (
            {
                "id": provisional.id,
                "company_name": provisional.company_name,
                "email": provisional.email,
                "phone": provisional.phone,
                "gstin": provisional.gstin,
            }
            if provisional
            else None
        ),
        "candidate_customer": (
            {
                "id": candidate.id,
                "company_name": candidate.company_name,
                "email": candidate.email,
                "phone": candidate.phone,
                "gstin": candidate.gstin,
            }
            if candidate
            else None
        ),
        "resolved_by": review.resolved_by,
        "resolution_notes": review.resolution_notes,
        "resolved_at": review.resolved_at,
        "created_at": review.created_at,
    }


@app.get("/customers/match-reviews")
async def list_customer_match_reviews(
    business_id: str,
    status: CustomerMatchReviewStatus = CustomerMatchReviewStatus.PENDING,
):
    async with SessionFactory() as session:
        reviews = (
            await session.execute(
                select(CustomerMatchReview)
                .where(
                    CustomerMatchReview.business_id == business_id,
                    CustomerMatchReview.status == status,
                )
                .order_by(CustomerMatchReview.created_at.desc())
            )
        ).scalars().all()

        payload = []
        for review in reviews:
            provisional = await session.get(
                Customer, review.provisional_customer_id
            )
            candidate = await session.get(
                Customer, review.candidate_customer_id
            )
            payload.append(
                _customer_review_payload(review, provisional, candidate)
            )
        return {"items": payload, "count": len(payload)}


@app.post("/customers/match-reviews/{review_id}/resolve")
async def resolve_customer_match(
    review_id: str,
    request: CustomerMatchDecisionRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
):
    try:
        claim = await claim_request(
            business_id=request.business_id,
            endpoint=f"/customers/match-reviews/{review_id}/resolve",
            idempotency_key=idempotency_key,
            payload=request.model_dump(),
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claim.is_cached:
        return claim.cached_response

    async with SessionFactory() as session:
        try:
            review = await resolve_customer_match_review(
                session,
                review_id=review_id,
                business_id=request.business_id,
                action=request.action,
                resolved_by=request.resolved_by,
                notes=request.notes,
            )
            provisional = await session.get(
                Customer, review.provisional_customer_id
            )
            candidate = await session.get(
                Customer, review.candidate_customer_id
            )
            response = _customer_review_payload(
                review, provisional, candidate
            )
            await complete_request(
                claim.event_id, jsonable_encoder(response)
            )
            return response
        except ValueError as exc:
            await fail_request(claim.event_id, exc)
            raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/customers/{customer_id}/360")
async def customer_360(
    customer_id: str,
    business_id: str,
):
    async with SessionFactory() as session:
        try:
            return await get_customer_360(
                session,
                business_id=business_id,
                customer_id=customer_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/customers/{customer_id}/notes")
async def add_customer_semantic_note(
    customer_id: str,
    request: CustomerNoteRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
):
    if vector_store is None or embedding_service is None:
        raise HTTPException(
            status_code=503,
            detail="Semantic-memory services are not initialized.",
        )
    async with SessionFactory() as session:
        customer = await session.scalar(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.business_id == request.business_id,
            )
        )
        if customer is None:
            raise HTTPException(status_code=404, detail="Customer not found.")

    try:
        claim = await claim_request(
            business_id=request.business_id,
            endpoint=f"/customers/{customer_id}/notes",
            idempotency_key=idempotency_key,
            payload=request.model_dump(),
            thread_id=request.thread_id,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claim.is_cached:
        return claim.cached_response

    try:
        from app.customers.semantic_memory import save_customer_note

        note_id = await save_customer_note(
            vector_store=vector_store,
            embedding_service=embedding_service,
            business_id=request.business_id,
            customer_id=customer_id,
            content=request.content,
            content_type=request.content_type,
            thread_id=request.thread_id,
            interaction_id=request.interaction_id,
        )
        response = {"note_id": note_id, "customer_id": customer_id}
        await complete_request(
            claim.event_id,
            response,
            thread_id=request.thread_id,
            interaction_id=request.interaction_id,
        )
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise HTTPException(
            status_code=500, detail=f"Unable to save customer note: {exc}"
        ) from exc
