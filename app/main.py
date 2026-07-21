import json
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, Field

from app.documents.embeddings import LocalEmbeddingService
from app.documents.models import (
    DocumentType,
    DocumentUploadMetadata,
)
from app.documents.service import DocumentIngestionService
from app.documents.vector_store import DocumentVectorStore
from app.documents.router import AgentDocumentRetriever
from app.rag.langgraph_adapter import LangGraphRAGAdapter
from app.rag.service import RAGContextService
from app.rag.query_builder import canonical_agent_name


embedding_service: LocalEmbeddingService
vector_store: DocumentVectorStore
ingestion_service: DocumentIngestionService
rag_adapter: LangGraphRAGAdapter


class RAGRetrieveRequest(BaseModel):
    business_id: str = Field(min_length=1)
    agent_name: str = Field(min_length=1)
    state: dict = Field(default_factory=dict)
    top_k: int = Field(default=5, ge=1, le=20)


embedding_service = None
vector_store = None
ingestion_service = None
rag_service = None
rag_adapter = None
sales_graph = None
gemini_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global embedding_service
    global vector_store
    global ingestion_service
    global rag_service
    global rag_adapter
    global sales_graph
    global gemini_client

    embedding_service = LocalEmbeddingService()

    vector_store = DocumentVectorStore(
        embedding_dimension=embedding_service.dimension,
        path="qdrant_data",
    )

    ingestion_service = DocumentIngestionService(
        embedding_service=embedding_service,
        vector_store=vector_store,
        upload_root="uploads",
    )

    rag_service = RAGContextService(
        vector_store=vector_store,
        embedding_service=embedding_service,
    )

    rag_adapter = LangGraphRAGAdapter(
        rag_service=rag_service,
    )

    gemini_client = genai.Client(
        api_key=settings.GEMINI_API_KEY
    )

    sales_graph = build_graph(
        session_factory=Session,
        rag_adapter=rag_adapter,
        client=gemini_client,
    )

    yield

    vector_store.client.close()


app = FastAPI(
    title="AI Sales Operations Agent",
    lifespan=lifespan,
)

class InquiryRequest(BaseModel):
    business_id: str
    source: str
    raw_text: str
    sender_identifier: str | None = None


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "qdrant": "local", "rag": "ready"}


@app.post("/rag/retrieve")
async def retrieve_agent_context(request: RAGRetrieveRequest):
    state = {**request.state, "business_id": request.business_id}
    try:
        context = await rag_adapter.get_context(
            agent_name=request.agent_name,
            state=state,
            top_k=request.top_k,
        )
        return context.model_dump()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/documents/upload")
async def upload_document(
    business_id: str = Form(...),
    document_type: DocumentType = Form(...),
    allowed_agents_json: str = Form(...),
    version: str = Form("1.0"),
    file: UploadFile = File(...),
):
    try:
        allowed_agents = json.loads(allowed_agents_json)

        if not isinstance(allowed_agents, list):
            raise ValueError(
                "allowed_agents_json must contain a JSON list"
            )
        if not all(isinstance(name, str) and name.strip() for name in allowed_agents):
            raise ValueError("allowed_agents_json entries must be non-empty strings")
        allowed_agents = [canonical_agent_name(name.strip()) for name in allowed_agents]

        metadata = DocumentUploadMetadata(
            business_id=business_id,
            document_type=document_type,
            allowed_agents=allowed_agents,
            version=version,
        )

        return await ingestion_service.ingest(
            file=file,
            metadata=metadata,
        )

    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

@app.post("/inquiries/process")
async def process_inquiry(
    request: InquiryRequest,
):
    initial_state = {
        "business_id": request.business_id,
        "source": request.source,
        "raw_text": request.raw_text,
        "sender_identifier": request.sender_identifier,

        "stages_completed": [],
        "human_approval_reasons": [],

        "inquiry_id": None,
        "lead_id": None,
        "quotation_number": None,

        "extraction": None,
        "requirement": None,
        "customer_profile": None,
        "qualification": None,
        "feasibility": None,
        "pricing": None,

        "needs_followup": False,
        "needs_human_approval": False,
        "human_approval_stage": None,
        "error": None,
    }

    result = await sales_graph.ainvoke(
        initial_state
    )

    return result