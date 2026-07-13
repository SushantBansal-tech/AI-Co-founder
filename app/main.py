import json
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    File,
    Form,
    HTTPException,
    UploadFile,
)

from documents.embbedings import LocalEmbeddingService
from documents.models import (
    DocumentType,
    DocumentUploadMetadata,
)
from documents.service import DocumentIngestionService
from documents.vector_store import DocumentVectorStore


embedding_service: LocalEmbeddingService
vector_store: DocumentVectorStore
ingestion_service: DocumentIngestionService


@asynccontextmanager
async def lifespan(app :FastAPI):
    global embedding_service
    global vector_store
    global ingestion_service

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

    yield


app = FastAPI(
    title="AI Sales Operations Agent",
    lifespan=lifespan,
)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


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