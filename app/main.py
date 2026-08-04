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
    Query,
    UploadFile,
)
from fastapi.encoders import jsonable_encoder
from google import genai
#from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
from sqlalchemy import select, text, update
# from sqlalchemy.ext.asyncio import (
#     AsyncSession,
#     async_sessionmaker,
#     create_async_engine,
# )
load_dotenv()
from app.database import (
    Base,
    BusinessEvent,
    Customer,
    CustomerMatchReview,
    CustomerMatchReviewStatus,
    FollowUpJob,
    Interaction,
    SessionFactory,
    dispose_database_engine,
    engine as database_engine,
)
from app.customers.merge_service import resolve_customer_match_review
from app.customers.customer_360 import get_customer_360
from app.channels.service import ChannelIngestionService
from app.channels.configuration import channel_configuration_errors
from app.channels.email_adapter import EmailPollingService
from app.channels.jobs import ChannelJobService
from app.channels.outbound import ChannelOutboundDispatcher
from app.channels.website import router as website_channel_router
from app.channels.whatsapp import router as whatsapp_channel_router
from app.events.interactions import record_interaction
from app.events.service import record_business_event
from app.followups.jobs import FollowUpJobService
from app.followups.service import cancel_open_followup_jobs
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
from app.structured_documents import (
    StructuredDataRepository,
    StructuredDocumentIngestionService,
)
from app.graph2 import (
    build_complete_graph,
    make_initial_state,
)
from app.rag.langgraph_adapter import LangGraphRAGAdapter
from app.rag.query_builder import canonical_agent_name
from app.rag.service import RAGContextService
from app.sales_context import (
    CustomerMemoryService,
    MemoryOutboxWorker,
    SalesContextService,
)
from langgraph.checkpoint.postgres.aio import (
    AsyncPostgresSaver,
)


if sys.platform == "win32":
    asyncio.set_event_loop_policy(
        asyncio.WindowsSelectorEventLoopPolicy()
    )


# Load GEMINI_API_KEY and other environment variables from .env.
#load_dotenv()


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
channel_stop_event: asyncio.Event | None = None
channel_background_tasks: list[asyncio.Task] = []
channel_config_errors: list[str] = []
followup_job_service: FollowUpJobService | None = None
sales_context_service: SalesContextService | None = None
customer_memory_service: CustomerMemoryService | None = None
memory_outbox_worker: MemoryOutboxWorker | None = None


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
        "requirement",
        "feasibility",
        "pricing",
        "negotiation",
        "po",
        "po_revalidation",
    ]


class PricingRetryRequest(BaseModel):
    business_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)


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


class FollowUpJobActionRequest(BaseModel):
    business_id: str = Field(min_length=1)
    reason: str | None = None


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
    global channel_stop_event
    global channel_background_tasks
    global channel_config_errors
    global followup_job_service
    global sales_context_service
    global customer_memory_service
    global memory_outbox_worker
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

    structured_ingestion = StructuredDocumentIngestionService(
        SessionFactory
    )
    structured_data = StructuredDataRepository(SessionFactory)
    ingestion_service = DocumentIngestionService(
        embedding_service=embedding_service,
        vector_store=vector_store,
        upload_root="uploads",
        structured_ingestion_service=structured_ingestion,
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

    sales_context_service = SalesContextService(
        session_factory=SessionFactory,
        embedding_service=embedding_service,
        vector_store=vector_store,
    )
    customer_memory_service = CustomerMemoryService(SessionFactory)
    memory_outbox_worker = MemoryOutboxWorker(
        session_factory=SessionFactory,
        embedding_service=embedding_service,
        vector_store=vector_store,
        max_attempts=int(os.getenv("MEMORY_OUTBOX_MAX_ATTEMPTS", "5")),
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
        outbound_dispatcher=ChannelOutboundDispatcher(
            session_factory=SessionFactory,
        ),
        structured_data=structured_data,
        sales_context_service=sales_context_service,
    )
    app.state.channel_ingestion_service = ChannelIngestionService(
        session_factory=SessionFactory,
        sales_graph=sales_graph,
        initial_state_factory=make_initial_state,
    )
    app.state.session_factory = SessionFactory
    channel_job_service = ChannelJobService(
        session_factory=SessionFactory,
        ingestion_service=app.state.channel_ingestion_service,
    )
    app.state.channel_job_service = channel_job_service
    followup_job_service = FollowUpJobService(
        session_factory=SessionFactory,
        sales_graph=sales_graph,
    )
    app.state.followup_job_service = followup_job_service

    if DATABASE_URL.startswith("sqlite"):
        async with database_engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    email_poller_enabled = os.getenv(
        "EMAIL_POLLER_ENABLED", "false"
    ).lower() in {"1", "true", "yes"}
    channel_config_errors = await channel_configuration_errors(
        session_factory=SessionFactory,
        email_poller_enabled=email_poller_enabled,
    )
    if (
        channel_config_errors
        and os.getenv(
            "CHANNEL_CONFIGURATION_STRICT", "false"
        ).lower() in {"1", "true", "yes"}
    ):
        raise RuntimeError(
            "Invalid channel configuration: "
            + " ".join(channel_config_errors)
        )

    channel_stop_event = asyncio.Event()
    channel_background_tasks = []
    if os.getenv("CHANNEL_WORKER_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
    }:
        channel_background_tasks.append(
            asyncio.create_task(
                channel_job_service.run(channel_stop_event),
                name="channel-inbound-worker",
            )
        )

    if os.getenv("FOLLOWUP_WORKER_ENABLED", "true").lower() in {
        "1",
        "true",
        "yes",
    }:
        channel_background_tasks.append(
            asyncio.create_task(
                followup_job_service.run(
                    channel_stop_event,
                    idle_seconds=float(
                        os.getenv(
                            "FOLLOWUP_WORKER_IDLE_SECONDS",
                            "5",
                        )
                    ),
                    stale_seconds=int(
                        os.getenv(
                            "FOLLOWUP_STALE_SECONDS",
                            "300",
                        )
                    ),
                    reconcile_seconds=float(
                        os.getenv(
                            "FOLLOWUP_RECONCILE_SECONDS",
                            "600",
                        )
                    ),
                ),
                name="durable-followup-worker",
            )
        )

    if os.getenv("MEMORY_WORKER_ENABLED", "true").lower() in {
        "1", "true", "yes",
    }:
        channel_background_tasks.append(
            asyncio.create_task(
                memory_outbox_worker.run(
                    channel_stop_event,
                    idle_seconds=float(os.getenv("MEMORY_WORKER_IDLE_SECONDS", "2")),
                    stale_seconds=int(os.getenv("MEMORY_OUTBOX_STALE_SECONDS", "300")),
                ),
                name="customer-memory-outbox-worker",
            )
        )

    if email_poller_enabled and not channel_config_errors:
        email_poller = EmailPollingService(
            session_factory=SessionFactory,
            job_service=channel_job_service,
        )
        app.state.email_polling_service = email_poller
        channel_background_tasks.append(
            asyncio.create_task(
                email_poller.run(
                    channel_stop_event,
                    interval_seconds=float(
                        os.getenv("EMAIL_POLL_INTERVAL_SECONDS", "30")
                    ),
                ),
                name="email-imap-poller",
            )
        )

    try:
        yield

    finally:
        # --------------------------------------------------------------
        # Clean shutdown.
        # --------------------------------------------------------------

        if channel_stop_event is not None:
            channel_stop_event.set()
        if channel_background_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        *channel_background_tasks,
                        return_exceptions=True,
                    ),
                    timeout=10,
                )
            except TimeoutError:
                for task in channel_background_tasks:
                    task.cancel()
                await asyncio.gather(
                    *channel_background_tasks,
                    return_exceptions=True,
                )

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
app.include_router(website_channel_router)
app.include_router(whatsapp_channel_router)


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    database_ready = False
    try:
        async with SessionFactory() as session:
            await session.execute(text("SELECT 1"))
        database_ready = True
    except Exception:
        database_ready = False
    return {
        "status": (
            "ok"
            if database_ready and sales_graph is not None
            else "degraded"
        ),
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
            if database_ready
            else "unavailable"
        ),
        "channels": {
            "ready": not channel_config_errors,
            "configuration_errors": channel_config_errors,
            "email_poller_enabled": os.getenv(
                "EMAIL_POLLER_ENABLED", "false"
            ).lower() in {"1", "true", "yes"},
            "worker_enabled": os.getenv(
                "CHANNEL_WORKER_ENABLED", "true"
            ).lower() in {"1", "true", "yes"},
        },
        "followups": {
            "worker_enabled": os.getenv(
                "FOLLOWUP_WORKER_ENABLED", "true"
            ).lower() in {"1", "true", "yes"},
            "worker_ready": followup_job_service is not None,
        },
    }


# ---------------------------------------------------------------------------
# Durable follow-up job operations
# ---------------------------------------------------------------------------

def _followup_job_payload(job: FollowUpJob) -> dict:
    return {
        "id": job.id,
        "business_id": job.business_id,
        "thread_id": job.thread_id,
        "quotation_id": job.quotation_id,
        "quotation_number": job.quotation_number,
        "attempt_number": job.attempt_number,
        "followup_type": job.followup_type,
        "tone": job.tone,
        "channel": job.channel,
        "recipient": job.recipient,
        "scheduled_for": job.scheduled_for,
        "next_attempt_at": job.next_attempt_at,
        "status": job.status,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "provider_message_id": job.provider_message_id,
        "last_error": job.last_error,
        "completed_at": job.completed_at,
        "cancelled_at": job.cancelled_at,
        "cancellation_reason": job.cancellation_reason,
    }


@app.get("/followups/jobs")
async def list_followup_jobs(
    business_id: str = Query(min_length=1),
    status_filter: str | None = Query(
        default=None,
        alias="status",
    ),
):
    statement = select(FollowUpJob).where(
        FollowUpJob.business_id == business_id
    )
    if status_filter:
        statement = statement.where(
            FollowUpJob.status == status_filter
        )
    statement = statement.order_by(
        FollowUpJob.scheduled_for,
        FollowUpJob.attempt_number,
    )
    async with SessionFactory() as session:
        jobs = (
            await session.execute(statement)
        ).scalars().all()
    return [
        jsonable_encoder(_followup_job_payload(job))
        for job in jobs
    ]


@app.get("/followups/jobs/{job_id}")
async def get_followup_job(
    job_id: str,
    business_id: str = Query(min_length=1),
):
    async with SessionFactory() as session:
        job = await session.scalar(
            select(FollowUpJob).where(
                FollowUpJob.id == job_id,
                FollowUpJob.business_id == business_id,
            )
        )
    if job is None:
        raise HTTPException(
            status_code=404,
            detail="Follow-up job not found.",
        )
    return jsonable_encoder(_followup_job_payload(job))


async def _followup_action_claim(
    *,
    endpoint: str,
    job_id: str,
    request: FollowUpJobActionRequest,
    idempotency_key: str,
):
    async with SessionFactory() as session:
        exists = await session.scalar(
            select(FollowUpJob.id).where(
                FollowUpJob.id == job_id,
                FollowUpJob.business_id == request.business_id,
            )
        )
    if not exists:
        raise HTTPException(
            status_code=404,
            detail="Follow-up job not found.",
        )
    try:
        return await claim_request(
            business_id=request.business_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            payload={
                "job_id": job_id,
                **request.model_dump(),
            },
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/followups/jobs/{job_id}/cancel")
async def cancel_followup_job(
    job_id: str,
    request: FollowUpJobActionRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
    ),
):
    if followup_job_service is None:
        raise HTTPException(
            status_code=503,
            detail="Follow-up worker is not initialized.",
        )
    endpoint = f"/followups/jobs/{job_id}/cancel"
    claim = await _followup_action_claim(
        endpoint=endpoint,
        job_id=job_id,
        request=request,
        idempotency_key=idempotency_key,
    )
    if claim.is_cached:
        return claim.cached_response
    changed = await followup_job_service.cancel_job(
        job_id,
        business_id=request.business_id,
        reason=request.reason or "Cancelled by operator.",
    )
    if not changed:
        await fail_request(
            claim.event_id,
            RuntimeError("Job is not cancellable."),
        )
        raise HTTPException(
            status_code=409,
            detail="Follow-up job is not cancellable.",
        )
    response = {"job_id": job_id, "status": "cancelled"}
    await complete_request(
        claim.event_id,
        response,
        response_status=200,
    )
    return response


@app.post("/followups/jobs/{job_id}/retry")
async def retry_followup_job(
    job_id: str,
    request: FollowUpJobActionRequest,
    idempotency_key: str = Header(
        ...,
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
    ),
):
    if followup_job_service is None:
        raise HTTPException(
            status_code=503,
            detail="Follow-up worker is not initialized.",
        )
    endpoint = f"/followups/jobs/{job_id}/retry"
    claim = await _followup_action_claim(
        endpoint=endpoint,
        job_id=job_id,
        request=request,
        idempotency_key=idempotency_key,
    )
    if claim.is_cached:
        return claim.cached_response
    changed = await followup_job_service.retry_job(
        job_id,
        business_id=request.business_id,
    )
    if not changed:
        await fail_request(
            claim.event_id,
            RuntimeError("Job is not retryable."),
        )
        raise HTTPException(
            status_code=409,
            detail="Follow-up job is not retryable.",
        )
    response = {"job_id": job_id, "status": "retry"}
    await complete_request(
        claim.event_id,
        response,
        response_status=200,
    )
    return response


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


def _raise_graph_error(result: dict) -> None:
    if result.get("error") and result.get("pipeline_status") == "failed":
        raise RuntimeError(result["error"])


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
        _raise_graph_error(result)

        async with SessionFactory() as session:
            await session.execute(
                update(Interaction)
                .where(Interaction.id == interaction.id)
                .values(
                    customer_id=result.get("customer_id"),
                    lead_id=result.get("lead_id"),
                )
            )
            await session.execute(
                update(BusinessEvent)
                .where(
                    BusinessEvent.business_id == request.business_id,
                    BusinessEvent.thread_id == thread_id,
                    BusinessEvent.customer_id.is_(None),
                )
                .values(
                    customer_id=result.get("customer_id"),
                    lead_id=result.get("lead_id"),
                )
            )
            await session.commit()

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
    if current_status == "awaiting_approval" or current_status.startswith("awaiting_approval:"):
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
            if request.trigger in {
                "customer_reply",
                "po_received",
            }:
                await cancel_open_followup_jobs(
                    session,
                    business_id=request.business_id,
                    thread_id=request.thread_id,
                    reason=(
                        "Customer replied."
                        if request.trigger == "customer_reply"
                        else "Purchase order received."
                    ),
                )
            await session.commit()
        result = await sales_graph.ainvoke(
            event_state,
            config=_graph_config(request.thread_id),
        )
        _raise_graph_error(result)
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

@app.post("/pipeline/pricing/retry")
async def retry_pipeline_pricing(
    request: PricingRetryRequest,
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=8, max_length=200
    ),
):
    current_state = await _load_thread_state(request.thread_id)
    _validate_thread_business(current_state, request.business_id)
    failure = current_state.get("failure") or {}
    pricing_blocked = (
        current_state.get("pipeline_status") == "blocked:pricing_data"
        or (
            current_state.get("pipeline_status") == "blocked"
            and failure.get("code") == "PRICING_DATA_MISSING"
        )
    )
    if not pricing_blocked:
        raise HTTPException(
            status_code=409,
            detail="Pricing retry is only allowed when pricing master data is blocked.",
        )
    try:
        claim = await claim_request(
            business_id=request.business_id,
            endpoint="/pipeline/pricing/retry",
            idempotency_key=idempotency_key,
            payload=request.model_dump(),
            thread_id=request.thread_id,
        )
    except (IdempotencyConflict, IdempotencyInProgress) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if claim.is_cached:
        return claim.cached_response
    try:
        result = await sales_graph.ainvoke(
            {
                "business_id": request.business_id,
                "trigger": "retry_pricing",
                "error": None,
                "needs_human_approval": False,
                "human_approval_stage": None,
            },
            config=_graph_config(request.thread_id),
        )
        _raise_graph_error(result)
        response = {"thread_id": request.thread_id, "state": result}
        await complete_request(
            claim.event_id, jsonable_encoder(response),
            thread_id=request.thread_id,
        )
        return response
    except Exception as exc:
        await fail_request(claim.event_id, exc)
        raise HTTPException(
            status_code=500,
            detail=f"Pricing retry failed: {exc}",
        ) from exc

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

    expected_status = f"awaiting_approval:{request.approved_stage}"
    current_status = current_state.get("pipeline_status")
    current_stage = current_state.get("human_approval_stage")

    if (
        current_status not in {expected_status, "awaiting_approval"}
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
        _raise_graph_error(result)
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


@app.get("/customers/{customer_id}/sales-context")
async def customer_sales_context(
    customer_id: str,
    business_id: str,
    agent_name: str = "customer_qualification",
    query: str | None = None,
    top_k: int = Query(default=5, ge=1, le=20),
):
    if sales_context_service is None:
        raise HTTPException(status_code=503, detail="Sales context is not initialized.")
    try:
        context = await sales_context_service.get_context(
            business_id=business_id,
            customer_id=customer_id,
            agent_name=canonical_agent_name(agent_name),
            query=query,
            top_k=top_k,
        )
        return context.model_dump()
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
    if customer_memory_service is None:
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
        note_id, outbox_id = await customer_memory_service.create_note(
            business_id=request.business_id,
            customer_id=customer_id,
            content=request.content,
            content_type=request.content_type,
            thread_id=request.thread_id,
            interaction_id=request.interaction_id,
            request_event_id=claim.event_id,
        )
        response = {
            "note_id": note_id,
            "customer_id": customer_id,
            "memory_outbox_id": outbox_id,
            "memory_status": "queued",
        }
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
