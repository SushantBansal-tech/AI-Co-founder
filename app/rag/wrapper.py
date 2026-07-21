import inspect
from collections.abc import Callable
from typing import Any

from app.rag.langgraph_adapter import LangGraphRAGAdapter


def with_rag_context(
    *,
    agent_name: str,
    rag_adapter: LangGraphRAGAdapter,
    agent_handler: Callable,
):
    """Wrap a sync or async LangGraph node with agent-scoped RAG context."""

    async def wrapped_node(state: dict[str, Any]) -> dict:
        rag_context = await rag_adapter.get_context(
            agent_name=agent_name,
            state=state,
        )
        result = agent_handler(state=state, rag_context=rag_context)
        if inspect.isawaitable(result):
            result = await result

        return {
            **result,
            f"{agent_name}_evidence_chunk_ids": rag_context.chunk_ids,
            f"{agent_name}_retrieval_query": rag_context.query,
        }

    return wrapped_node
