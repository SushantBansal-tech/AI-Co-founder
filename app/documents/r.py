from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
)


class EmbeddingService(Protocol):
    """
    Any embedding service used here must generate vectors with the same
    model and dimension used when the documents were inserted into Qdrant.
    """

    def embed_query(self, text: str) -> list[float]:
        ...


class RetrievedCatalogProduct(BaseModel):
    product_code: str
    name: str
    category: str
    grade: str | None = None
    specifications: str | None = None
    unit: str = "MT"

    score: float = 0.0
    chunk_id: str | None = None
    document_id: str | None = None
    evidence_text: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_embed_text(self) -> str:
        parts = [self.name, self.category]

        if self.grade:
            parts.append(f"Grade {self.grade}")

        if self.specifications:
            parts.append(self.specifications)

        return " | ".join(parts)


class QdrantCatalogRetriever:
    def __init__(
        self,
        client: QdrantClient,
        embedding_service: EmbeddingService,
        collection_name: str = "sales_agent_documents",
    ) -> None:
        self.client = client
        self.embedding_service = embedding_service
        self.collection_name = collection_name

    def search_product_catalog(
        self,
        *,
        query_text: str,
        business_id: str,
        limit: int = 3,
    ) -> list[RetrievedCatalogProduct]:
        """
        Search product-catalog chunks belonging to one business.

        The collection must use the same embedding model and vector
        dimension as self.embedding_service.
        """

        cleaned_query = query_text.strip()

        if not cleaned_query:
            return []

        if limit <= 0:
            raise ValueError("limit must be greater than zero")

        query_vector = self.embedding_service.embed_query(cleaned_query)

        query_filter = Filter(
            must=[
                FieldCondition(
                    key="business_id",
                    match=MatchValue(value=business_id),
                ),
                FieldCondition(
                    key="document_type",
                    match=MatchAny(
                        any=[
                            "product_catalog",
                            "technical_specification",
                        ]
                    ),
                ),
                FieldCondition(
                    key="status",
                    match=MatchValue(value="active"),
                ),
                FieldCondition(
                    key="allowed_agents",
                    match=MatchValue(
                        value="requirement_understanding"
                    ),
                ),
            ]
        )

        result = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )

        products: list[RetrievedCatalogProduct] = []

        for point in result.points:
            payload = dict(point.payload or {})

            product = self._payload_to_product(
                payload=payload,
                score=float(point.score),
            )

            if product is not None:
                products.append(product)

        return products

    @staticmethod
    def _payload_to_product(
        *,
        payload: dict[str, Any],
        score: float,
    ) -> RetrievedCatalogProduct | None:
        """
        Convert a Qdrant payload into a typed product.

        Returns None when the retrieved chunk does not contain enough
        product metadata to represent a catalog product.
        """

        product_code = str(payload.get("product_code", "")).strip()
        name = str(payload.get("name", "")).strip()
        category = str(payload.get("category", "")).strip()

        if not product_code or not name:
            return None

        return RetrievedCatalogProduct(
            product_code=product_code,
            name=name,
            category=category or "Unspecified",
            grade=_optional_string(payload.get("grade")),
            specifications=_optional_string(
                payload.get("specifications")
            ),
            unit=str(payload.get("unit") or "MT"),
            score=score,
            chunk_id=_optional_string(payload.get("chunk_id")),
            document_id=_optional_string(payload.get("document_id")),
            evidence_text=str(
                payload.get("text")
                or payload.get("content")
                or payload.get("chunk_text")
                or ""
            ),
            payload=payload,
        )


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None

    text = str(value).strip()
    return text or None