from datetime import datetime
from typing import Optional

from app.documents.embeddings import LocalEmbeddingService
from app.documents.vector_store import DocumentVectorStore


async def save_customer_note(
    *,
    vector_store: DocumentVectorStore,
    embedding_service: LocalEmbeddingService,
    business_id: str,
    customer_id: str,
    content: str,
    content_type: str,
    thread_id: Optional[str] = None,
    interaction_id: Optional[str] = None,
) -> str:
    vector = await embedding_service.embed_query(content)
    return vector_store.upsert_customer_note(
        vector=vector,
        business_id=business_id,
        customer_id=customer_id,
        content=content,
        content_type=content_type,
        thread_id=thread_id,
        interaction_id=interaction_id,
        occurred_at=datetime.utcnow().isoformat(),
    )
