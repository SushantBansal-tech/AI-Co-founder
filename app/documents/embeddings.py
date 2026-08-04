import asyncio
from typing import Sequence

from sentence_transformers import SentenceTransformer


class LocalEmbeddingService:
    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
    ) -> None:
        self.model = SentenceTransformer(model_name)
        self.dimension = self.model.get_embedding_dimension()

    def _encode_sync(
        self,
        texts: Sequence[str],
    ) -> list[list[float]]:
        vectors = self.model.encode(
            list(texts),
            normalize_embeddings=True,
            show_progress_bar=False,
        )

        return vectors.tolist()

    async def embed_documents(
        self,
        texts: Sequence[str],
    ) -> list[list[float]]:
        # SentenceTransformer is synchronous and CPU-bound.
        # Run it outside the FastAPI event-loop thread.
        return await asyncio.to_thread(
            self._encode_sync,
            texts,
        )

    async def embed_query(
        self,
        query: str,
    ) -> list[float]:
        vectors = await self.embed_documents([query])
        return vectors[0]