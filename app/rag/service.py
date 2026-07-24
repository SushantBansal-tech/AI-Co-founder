from app.documents.router import AgentDocumentRetriever
from app.rag.models import AgentRAGContext, RetrievedChunk


class RAGContextService:
    """Build agent-scoped context using the application's document store."""

    def __init__(self, retriever: AgentDocumentRetriever) -> None:
        self.retriever = retriever

    async def retrieve_for_agent(
        self,
        *,
        agent_name: str,
        business_id: str,
        query: str,
        top_k: int = 5,
    ) -> AgentRAGContext:
        cleaned_query = query.strip()
        if not cleaned_query:
            return AgentRAGContext(agent_name=agent_name, query=query)

        results = await self.retriever.retrieve(
            agent_name=agent_name,
            business_id=business_id,
            query=cleaned_query,
            top_k=top_k,
        )
        chunks = [
            RetrievedChunk(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                document_type=item.document_type,
                text=item.chunk_text,
                score=item.similarity_score,
                metadata={
                    "document_name": item.document_name,
                    "page_number": item.page_number,
                    "sheet_name": item.sheet_name,
                },
            )
            for item in results
        ]
        return AgentRAGContext(
            agent_name=agent_name,
            query=cleaned_query,
            chunks=chunks,
        )

    async def retrieve_document_for_agent(
        self,
        *,
        agent_name: str,
        business_id: str,
        document_name: str,
        document_type: str,
    ) -> AgentRAGContext:
        results = await self.retriever.retrieve_document(
            agent_name=agent_name,
            business_id=business_id,
            document_name=document_name,
            document_type=document_type,
        )
        chunks = [
            RetrievedChunk(
                chunk_id=item.chunk_id,
                document_id=item.document_id,
                document_type=item.document_type,
                text=item.chunk_text,
                score=1.0,
                metadata={
                    "document_name": item.document_name,
                    "chunk_index": item.chunk_index,
                    "version": item.version,
                    "structured_lookup": True,
                },
            )
            for item in results
        ]
        return AgentRAGContext(
            agent_name=agent_name,
            query=(
                "Exact structured document lookup: "
                f"{document_type}/{document_name}"
            ),
            chunks=chunks,
        )
