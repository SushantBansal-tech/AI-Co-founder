# app/rag/langgraph_adapter.py

from typing import Any

from app.rag.query_builder import AgentQueryBuilder
from app.rag.service import RAGContextService


class LangGraphRAGAdapter:
    def __init__(
        self,
        rag_service: RAGContextService,
    ) -> None:
        self.rag_service = rag_service

    def get_context(
        self,
        *,
        agent_name: str,
        state: dict[str, Any],
    ):
        query = AgentQueryBuilder.build(
            agent_name=agent_name,
            state=state,
        )

        return self.rag_service.retrieve_for_agent(
            agent_name=agent_name,
            business_id=state["business_id"],
            query=query,
        )