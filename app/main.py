import json
import os
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
    UploadFile,
)
from google import genai
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
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

database_engine = None
Session = None

sales_graph = None
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
    global database_engine
    global Session
    global sales_graph
    global graph_checkpointer
    global gemini_client

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

    database_engine = create_async_engine(
        "sqlite+aiosqlite:///sales_os.db",
    )

    Session = async_sessionmaker(
        database_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    # ------------------------------------------------------------------
    # 7. Build the complete LangGraph.
    #
    # Building the graph imports the agent modules. Those modules register
    # their SQLAlchemy models on the shared Base metadata.
    # ------------------------------------------------------------------

    graph_checkpointer = MemorySaver()

    sales_graph = build_complete_graph(
        session_factory=Session,
        rag_adapter=rag_adapter,
        client=gemini_client,
        checkpointer=graph_checkpointer,
    )

    # ------------------------------------------------------------------
    # 8. Create all registered database tables.
    # ------------------------------------------------------------------

    inquiry_module = import_module("01_Inquiry")

    async with database_engine.begin() as connection:
        await connection.run_sync(
            inquiry_module.Base.metadata.create_all
        )

    try:
        yield

    finally:
        # --------------------------------------------------------------
        # Clean shutdown.
        # --------------------------------------------------------------

        if vector_store is not None:
            vector_store.client.close()

        if database_engine is not None:
            await database_engine.dispose()


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
            if Session is not None
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
):
    if ingestion_service is None:
        raise HTTPException(
            status_code=503,
            detail="Document ingestion service is not initialized.",
        )

    try:
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

        return result

    except (ValueError, json.JSONDecodeError) as exc:
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
):
    if sales_graph is None:
        raise HTTPException(
            status_code=503,
            detail="Sales graph is not initialized.",
        )

    initial_state = make_initial_state(
        trigger="inquiry",
        business_id=request.business_id,
        source=request.source,
        raw_text=request.raw_text,
        sender_identifier=request.sender_identifier,
    )

    thread_id = str(uuid4())
    config = _graph_config(thread_id)

    try:
        result = await sales_graph.ainvoke(
            initial_state,
            config=config,
        )

        return {
            "thread_id": thread_id,
            "state": result,
        }

    except Exception as exc:
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
        result = await sales_graph.ainvoke(
            event_state,
            config=_graph_config(request.thread_id),
        )
        return {
            "thread_id": request.thread_id,
            "state": result,
        }
    except HTTPException:
        raise
    except Exception as exc:
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
        return {
            "thread_id": request.thread_id,
            "approved_stage": request.approved_stage,
            "state": result,
        }
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Pipeline approval failed: {exc}",
        ) from exc
