import re
import uuid

from qdrant_client import QdrantClient, models

from app.documents.models import DocumentChunk, RetrievedChunk


class DocumentVectorStore:
    COLLECTION_NAME = "sales_agent_documents"
    CUSTOMER_MEMORY_COLLECTION = "customer_semantic_memory"

    def __init__(
        self,
        embedding_dimension: int,
        path: str = "qdrant_data",
    ) -> None:
        # Local persisted Qdrant mode.
        # For production, replace this with a Qdrant server URL/API key.
        self.client = QdrantClient(path=path)
        self.embedding_dimension = embedding_dimension

        self._ensure_collection()
        self._ensure_customer_memory_collection()

    def _ensure_collection(self) -> None:
        if self.client.collection_exists(self.COLLECTION_NAME):
            return

        self.client.create_collection(
            collection_name=self.COLLECTION_NAME,
            vectors_config=models.VectorParams(
                size=self.embedding_dimension,
                distance=models.Distance.COSINE,
            ),
        )

        # Payload indexes improve metadata-filter performance.
        for field_name in [
            "business_id",
            "document_id",
            "document_type",
            "status",
        ]:
            self.client.create_payload_index(
                collection_name=self.COLLECTION_NAME,
                field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD,
            )

    def _ensure_customer_memory_collection(self) -> None:
        if not self.client.collection_exists(self.CUSTOMER_MEMORY_COLLECTION):
            self.client.create_collection(
                collection_name=self.CUSTOMER_MEMORY_COLLECTION,
                vectors_config=models.VectorParams(
                    size=self.embedding_dimension,
                    distance=models.Distance.COSINE,
                ),
            )
        for field_name in (
            "business_id", "customer_id", "thread_id",
            "interaction_id", "content_type",
        ):
            try:
                self.client.create_payload_index(
                    collection_name=self.CUSTOMER_MEMORY_COLLECTION,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
            except Exception:
                # Local Qdrant can report an existing payload index.
                pass

    def upsert_customer_note(
        self,
        *,
        vector: list[float],
        business_id: str,
        customer_id: str,
        content: str,
        content_type: str,
        thread_id: str | None = None,
        interaction_id: str | None = None,
        occurred_at: str | None = None,
        note_id: str | None = None,
        source_type: str | None = None,
        source_id: str | None = None,
    ) -> str:
        point_id = note_id or str(uuid.uuid4())
        self.client.upsert(
            collection_name=self.CUSTOMER_MEMORY_COLLECTION,
            points=[
                models.PointStruct(
                    id=point_id,
                    vector=vector,
                    payload={
                        "business_id": business_id,
                        "customer_id": customer_id,
                        "thread_id": thread_id,
                        "interaction_id": interaction_id,
                        "content_type": content_type,
                        "content": content,
                        "occurred_at": occurred_at,
                        "source_type": source_type,
                        "source_id": source_id,
                    },
                )
            ],
            wait=True,
        )
        return point_id

    def search_customer_memory(
        self,
        *,
        query_vector: list[float],
        business_id: str,
        customer_id: str,
        top_k: int = 5,
        score_threshold: float = 0.30,
    ) -> list[dict]:
        """Search only one customer's memory inside one tenant."""
        response = self.client.query_points(
            collection_name=self.CUSTOMER_MEMORY_COLLECTION,
            query=query_vector,
            query_filter=models.Filter(must=[
                models.FieldCondition(
                    key="business_id",
                    match=models.MatchValue(value=business_id),
                ),
                models.FieldCondition(
                    key="customer_id",
                    match=models.MatchValue(value=customer_id),
                ),
            ]),
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )
        memories = []
        for point in response.points:
            payload = point.payload or {}
            memories.append({
                "memory_id": str(point.id),
                "content": str(payload.get("content", "")),
                "content_type": str(payload.get("content_type", "note")),
                "score": float(point.score),
                "occurred_at": payload.get("occurred_at"),
                "thread_id": payload.get("thread_id"),
            })
        return memories

    def upsert_chunks(
        self,
        chunks: list[DocumentChunk],
        embeddings: list[list[float]],
    ) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError(
                "Number of chunks and embeddings must match"
            )

        points: list[models.PointStruct] = []

        for chunk, vector in zip(chunks, embeddings):
            payload = chunk.model_dump()

            points.append(
                models.PointStruct(
                    id=chunk.chunk_id,
                    vector=vector,
                    payload=payload,
                )
            )

        self.client.upsert(
            collection_name=self.COLLECTION_NAME,
            points=points,
            wait=True,
        )

    def search(
        self,
        *,
        query_vector: list[float],
        business_id: str,
        agent_name: str,
        allowed_document_types: list[str],
        top_k: int = 5,
        score_threshold: float = 0.35,
    ) -> list[RetrievedChunk]:
        conditions: list[models.Condition] = [
            models.FieldCondition(
                key="business_id",
                match=models.MatchValue(value=business_id),
            ),
            models.FieldCondition(
                key="status",
                match=models.MatchValue(value="active"),
            ),
            models.FieldCondition(
                key="document_type",
                match=models.MatchAny(
                    any=allowed_document_types
                ),
            ),
            models.FieldCondition(
                key="allowed_agents",
                match=models.MatchValue(value=agent_name),
            ),
        ]

        response = self.client.query_points(
            collection_name=self.COLLECTION_NAME,
            query=query_vector,
            query_filter=models.Filter(must=conditions),
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
        )

        retrieved: list[RetrievedChunk] = []

        for point in response.points:
            payload = point.payload or {}

            retrieved.append(
                RetrievedChunk(
                    chunk_id=str(payload.get("chunk_id", point.id)),
                    document_id=str(payload["document_id"]),
                    document_name=str(payload["document_name"]),
                    document_type=str(payload["document_type"]),
                    chunk_text=str(payload["chunk_text"]),
                    similarity_score=float(point.score),
                    page_number=payload.get("page_number"),
                    sheet_name=payload.get("sheet_name"),
                )
            )

        return retrieved

    def get_document_chunks(
        self,
        *,
        business_id: str,
        agent_name: str,
        document_name: str,
        document_type: str,
        page_size: int = 100,
    ) -> list[DocumentChunk]:
        """
        Retrieve a complete structured document by exact payload filters.

        Unlike semantic search, this method does not use embeddings, scores,
        or a top-k limit. It is intended for CSV-backed deterministic lookups
        such as inventory, capacity, prices, margins, taxes and transport.
        When several active versions exist, only the lexicographically latest
        version is returned.
        """
        conditions: list[models.Condition] = [
            models.FieldCondition(
                key="business_id",
                match=models.MatchValue(value=business_id),
            ),
            models.FieldCondition(
                key="status",
                match=models.MatchValue(value="active"),
            ),
            models.FieldCondition(
                key="document_name",
                match=models.MatchValue(value=document_name),
            ),
            models.FieldCondition(
                key="document_type",
                match=models.MatchValue(value=document_type),
            ),
            models.FieldCondition(
                key="allowed_agents",
                match=models.MatchValue(value=agent_name),
            ),
        ]

        chunks: list[DocumentChunk] = []
        offset = None

        while True:
            points, offset = self.client.scroll(
                collection_name=self.COLLECTION_NAME,
                scroll_filter=models.Filter(must=conditions),
                limit=page_size,
                offset=offset,
                with_payload=True,
                with_vectors=False,
            )

            for point in points:
                payload = point.payload or {}
                chunks.append(DocumentChunk(**payload))

            if offset is None:
                break

        if not chunks:
            return []

        def version_key(version: str):
            return tuple(
                (0, int(part)) if part.isdigit() else (1, part.lower())
                for part in re.split(r"(\d+)", version)
                if part
            )

        latest_version = max(
            (chunk.version for chunk in chunks),
            key=version_key,
        )
        latest_chunks = [
            chunk
            for chunk in chunks
            if chunk.version == latest_version
        ]

        return sorted(
            latest_chunks,
            key=lambda chunk: (
                chunk.document_id,
                chunk.chunk_index,
            ),
        )

    def delete_document(
        self,
        document_id: str,
        business_id: str,
    ) -> None:
        self.client.delete(
            collection_name=self.COLLECTION_NAME,
            points_selector=models.FilterSelector(
                filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="document_id",
                            match=models.MatchValue(
                                value=document_id
                            ),
                        ),
                        models.FieldCondition(
                            key="business_id",
                            match=models.MatchValue(
                                value=business_id
                            ),
                        ),
                    ]
                )
            ),
            wait=True,
        )
