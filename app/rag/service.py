# app/rag/service.py

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
)

from app.rag.models import AgentRAGContext, RetrievedChunk
from app.rag.policy import AGENT_RETRIEVAL_POLICIES
from app.documents.service import DocumentIngestionService


class RAGContextService:
    def __init__(
        self,
        qdrant_client: QdrantClient,
        embedding_service: DocumentIngestionService,
        collection_name: str = "sales_agent_documents",
    ) -> None:
        self.qdrant_client = qdrant_client
        self.embedding_service = DocumentIngestionService
        self.collection_name = collection_name

    def retrieve_for_agent(
        self,
        *,
        agent_name: str,
        business_id: str,
        query: str,
    ) -> AgentRAGContext:
        policy = AGENT_RETRIEVAL_POLICIES.get(agent_name)

        if policy is None:
            raise ValueError(
                f"No retrieval policy configured for agent: {agent_name}"
            )

        cleaned_query = query.strip()

        if not cleaned_query:
            return AgentRAGContext(
                agent_name=agent_name,
                query=query,
                chunks=[],
            )

        query_vector = self.embedding_service.embed_query(cleaned_query)

        conditions = [
            FieldCondition(
                key="business_id",
                match=MatchValue(value=business_id),
            ),
            FieldCondition(
                key="document_type",
                match=MatchAny(any=policy.document_types),
            ),
            FieldCondition(
                key="status",
                match=MatchValue(value="active"),
            ),
        ]

        if policy.allowed_agents_filter:
            conditions.append(
                FieldCondition(
                    key="allowed_agents",
                    match=MatchValue(value=agent_name),
                )
            )

        result = self.qdrant_client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=Filter(must=conditions),
            limit=policy.top_k,
            score_threshold=policy.score_threshold,
            with_payload=True,
            with_vectors=False,
        )

        chunks: list[RetrievedChunk] = []

        for point in result.points:
            payload = dict(point.payload or {})

            text = (
                payload.get("text")
                or payload.get("chunk_text")
                or payload.get("content")
                or ""
            )

            if not text:
                continue

            chunks.append(
                RetrievedChunk(
                    chunk_id=str(
                        payload.get("chunk_id") or point.id
                    ),
                    document_id=str(
                        payload.get("document_id") or ""
                    ),
                    document_type=str(
                        payload.get("document_type") or "unknown"
                    ),
                    text=str(text),
                    score=float(point.score),
                    metadata=payload,
                )
            )

        return AgentRAGContext(
            agent_name=agent_name,
            query=cleaned_query,
            chunks=chunks,
        )