from app.customers.customer_360 import get_customer_360
from app.sales_context.models import MemorySnippet, SalesContext
from app.sales_context.query_builder import build_sales_memory_query


class SalesContextService:
    def __init__(self, *, session_factory, embedding_service, vector_store) -> None:
        self.session_factory = session_factory
        self.embedding_service = embedding_service
        self.vector_store = vector_store

    async def get_context(
        self, *, business_id: str, customer_id: str, agent_name: str,
        state: dict | None = None, query: str | None = None, top_k: int = 5,
    ) -> SalesContext:
        # PostgreSQL is authoritative and must succeed.
        async with self.session_factory() as session:
            customer_360 = await get_customer_360(
                session, business_id=business_id, customer_id=customer_id,
            )
        memory_query = query or build_sales_memory_query(agent_name, state or {})
        memories = []
        warnings = []
        available = True
        try:
            vector = await self.embedding_service.embed_query(memory_query)
            memories = self.vector_store.search_customer_memory(
                query_vector=vector,
                business_id=business_id,
                customer_id=customer_id,
                top_k=top_k,
            )
        except Exception as exc:
            # Qdrant enriches context; it must not block exact PostgreSQL facts.
            available = False
            warnings.append(f"Semantic memory unavailable: {exc}")
        return SalesContext(
            business_id=business_id,
            customer_id=customer_id,
            agent_name=agent_name,
            query=memory_query,
            customer_360=customer_360,
            semantic_memories=[MemorySnippet(**item) for item in memories],
            semantic_memory_available=available,
            warnings=warnings,
        )
