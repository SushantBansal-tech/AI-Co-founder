# app/rag/models.py

from typing import Any

from pydantic import BaseModel, Field


class RetrievedChunk(BaseModel):
    chunk_id: str
    document_id: str
    document_type: str
    text: str
    score: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentRAGContext(BaseModel):
    agent_name: str
    query: str
    chunks: list[RetrievedChunk] = Field(default_factory=list)

    @property
    def combined_text(self) -> str:
        if not self.chunks:
            return "No relevant company documents were found."

        sections = []

        for index, chunk in enumerate(self.chunks, start=1):
            sections.append(
                f"[Evidence {index}]\n"
                f"Document type: {chunk.document_type}\n"
                f"Score: {chunk.score:.4f}\n"
                f"Content: {chunk.text}"
            )

        return "\n\n".join(sections)

    @property
    def chunk_ids(self) -> list[str]:
        return [chunk.chunk_id for chunk in self.chunks]