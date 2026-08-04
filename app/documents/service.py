import re
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.documents.chunker import split_into_chunks
from app.documents.embeddings import LocalEmbeddingService
from app.documents.models import (
    DocumentChunk,
    DocumentUploadMetadata,
    ParsedDocument,
)
from app.documents.parser import parse_document
from app.documents.vector_store import DocumentVectorStore
from app.structured_documents.readers import read_tabular_rows


ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".txt",
    ".csv",
    ".xlsx",
}


def sanitize_filename(filename: str) -> str:
    filename = Path(filename).name
    return re.sub(r"[^a-zA-Z0-9._-]", "_", filename)


class DocumentIngestionService:
    def __init__(
        self,
        embedding_service: LocalEmbeddingService,
        vector_store: DocumentVectorStore,
        upload_root: str = "uploads",
        structured_ingestion_service=None,
    ) -> None:
        self.embedding_service = embedding_service
        self.vector_store = vector_store
        self.upload_root = Path(upload_root)
        self.structured_ingestion_service = structured_ingestion_service

    async def ingest(
        self,
        file: UploadFile,
        metadata: DocumentUploadMetadata,
    ) -> dict:
        if not file.filename:
            raise ValueError("Uploaded file must have a filename")

        safe_name = sanitize_filename(file.filename)
        extension = Path(safe_name).suffix.lower()

        if extension not in ALLOWED_EXTENSIONS:
            raise ValueError(
                f"Unsupported file type: {extension}"
            )

        document_id = str(uuid.uuid4())

        business_directory = (
            self.upload_root
            / metadata.business_id
            / document_id
        )
        business_directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        file_path = business_directory / safe_name

        # Save the original file.
        with file_path.open("wb") as output_file:
            while content := await file.read(1024 * 1024):
                output_file.write(content)

        if extension in {".csv", ".xlsx"}:
            if self.structured_ingestion_service is None:
                raise ValueError("Structured document ingestion is not configured")
            result = await self.structured_ingestion_service.ingest(
                path=str(file_path), filename=safe_name, metadata=metadata,
                document_id=document_id,
            )
            # Product vectors are a discovery index only. PostgreSQL remains
            # authoritative and exact verification happens in the graph.
            if metadata.document_type.value == "product_catalog":
                rows = read_tabular_rows(str(file_path))
                chunks = [DocumentChunk(
                    chunk_id=str(uuid.uuid4()), document_id=document_id,
                    business_id=metadata.business_id, document_name=safe_name,
                    document_type=metadata.document_type.value,
                    allowed_agents=metadata.allowed_agents, chunk_index=index,
                    chunk_text=(
                        f"Product code: {row.get('product_code','')} | "
                        f"Name: {row.get('name','')} | Category: {row.get('category','')} | "
                        f"Grade: {row.get('grade','')} | Specifications: {row.get('specifications','')} | "
                        f"Unit: {row.get('unit','MT')}"
                    ), version=metadata.version, status=metadata.status,
                    sheet_name="catalog_products",
                ) for index, row in enumerate(rows)]
                embeddings = await self.embedding_service.embed_documents(
                    [chunk.chunk_text for chunk in chunks]
                )
                self.vector_store.upsert_chunks(chunks=chunks, embeddings=embeddings)
                result["candidate_vector_count"] = len(chunks)
            return result

        text = parse_document(str(file_path))

        parsed_document = ParsedDocument(
            document_id=document_id,
            business_id=metadata.business_id,
            file_name=safe_name,
            file_path=str(file_path),
            document_type=metadata.document_type,
            allowed_agents=metadata.allowed_agents,
            text=text,
            version=metadata.version,
            status=metadata.status,
        )

        chunks = split_into_chunks(
            parsed_document,
            chunk_size=1200,
            overlap=200,
        )

        embeddings = await self.embedding_service.embed_documents(
            [chunk.chunk_text for chunk in chunks]
        )

        self.vector_store.upsert_chunks(
            chunks=chunks,
            embeddings=embeddings,
        )

        return {
            "status": "indexed",
            "document_id": document_id,
            "file_name": safe_name,
            "document_type": metadata.document_type.value,
            "allowed_agents": metadata.allowed_agents,
            "chunk_count": len(chunks),
        }
