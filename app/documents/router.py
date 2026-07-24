from app.documents.embeddings import LocalEmbeddingService
from app.documents.models import DocumentChunk, RetrievedChunk
from app.documents.policies import get_allowed_document_types
from app.documents.vector_store import DocumentVectorStore


class AgentDocumentRetriever:
    def __init__(
        self,
        embedding_service: LocalEmbeddingService,
        vector_store: DocumentVectorStore,
    ) -> None:
        self.embedding_service = embedding_service
        self.vector_store = vector_store

    async def retrieve(
        self,
        *,
        business_id: str,
        agent_name: str,
        query: str,
        top_k: int = 5,
    ) -> list[RetrievedChunk]:
        allowed_document_types = get_allowed_document_types(
            agent_name
        )

        query_embedding = await self.embedding_service.embed_query(
            query
        )

        return self.vector_store.search(
            query_vector=query_embedding,
            business_id=business_id,
            agent_name=agent_name,
            allowed_document_types=allowed_document_types,
            top_k=top_k,
        )

    async def retrieve_document(
        self,
        *,
        business_id: str,
        agent_name: str,
        document_name: str,
        document_type: str,
    ) -> list[DocumentChunk]:
        allowed_document_types = get_allowed_document_types(
            agent_name
        )
        if document_type not in allowed_document_types:
            raise ValueError(
                f"Agent {agent_name} is not allowed to access "
                f"document type {document_type}"
            )

        return self.vector_store.get_document_chunks(
            business_id=business_id,
            agent_name=agent_name,
            document_name=document_name,
            document_type=document_type,
        )
