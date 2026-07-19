from collections.abc import Callable
from typing import Any


def with_rag_context(
    *,
    agent_name: str,
    rag_adapter: LangGraphRAGAdapter,
    agent_handler: Callable,
):
    def wrapped_node(state: dict[str, Any]) -> dict:
        rag_context = rag_adapter.get_context(
            agent_name=agent_name,
            state=state,
        )

        result = agent_handler(
            state=state,
            rag_context=rag_context,
        )

        return {
            **result,
            f"{agent_name}_evidence_chunk_ids": (
                rag_context.chunk_ids
            ),
            f"{agent_name}_retrieval_query": rag_context.query,
        }

    return wrapped_node