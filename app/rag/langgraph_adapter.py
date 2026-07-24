# app/rag/langgraph_adapter.py

from typing import Any

from app.rag.query_builder import AgentQueryBuilder, canonical_agent_name
from app.rag.service import RAGContextService


class LangGraphRAGAdapter:
    def __init__(
        self,
        rag_service: RAGContextService,
    ) -> None:
        self.rag_service = rag_service

    async def get_context(
        self,
        *,
        agent_name: str,
        state: dict[str, Any],
        top_k: int = 5,
    ):
        canonical_name = canonical_agent_name(agent_name)
        query = AgentQueryBuilder.build(
            agent_name=canonical_name,
            state=state,
        )

        business_id = state.get("business_id")
        if not business_id:
            raise ValueError("LangGraph state must include business_id for RAG isolation")

        return await self.rag_service.retrieve_for_agent(
            agent_name=canonical_name,
            business_id=state["business_id"],
            query=query,
            top_k=top_k,
        )

    async def get_document_context(
        self,
        *,
        agent_name: str,
        state: dict[str, Any],
        document_name: str,
        document_type: str,
    ):
        canonical_name = canonical_agent_name(agent_name)
        business_id = state.get("business_id")
        if not business_id:
            raise ValueError(
                "LangGraph state must include business_id "
                "for structured document isolation"
            )

        return await self.rag_service.retrieve_document_for_agent(
            agent_name=canonical_name,
            business_id=business_id,
            document_name=document_name,
            document_type=document_type,
        )
