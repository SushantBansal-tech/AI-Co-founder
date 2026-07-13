import re
import uuid

from documents.models import DocumentChunk, ParsedDocument


def clean_text(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_into_chunks(
    document: ParsedDocument,
    chunk_size: int = 1200,
    overlap: int = 200,
) -> list[DocumentChunk]:
    if chunk_size <= overlap:
        raise ValueError("chunk_size must be greater than overlap")

    text = clean_text(document.text)

    chunks: list[DocumentChunk] = []
    start = 0
    chunk_index = 0

    while start < len(text):
        tentative_end = min(start + chunk_size, len(text))

        # Try to end at a paragraph or sentence boundary.
        end = tentative_end

        if tentative_end < len(text):
            paragraph_boundary = text.rfind(
                "\n\n",
                start,
                tentative_end,
            )
            sentence_boundary = text.rfind(
                ". ",
                start,
                tentative_end,
            )

            best_boundary = max(
                paragraph_boundary,
                sentence_boundary,
            )

            if best_boundary > start + chunk_size // 2:
                end = best_boundary + 1

        chunk_text = text[start:end].strip()

        if chunk_text:
            chunks.append(
                DocumentChunk(
                    chunk_id=str(uuid.uuid4()),
                    document_id=document.document_id,
                    business_id=document.business_id,
                    document_name=document.file_name,
                    document_type=document.document_type.value,
                    allowed_agents=document.allowed_agents,
                    chunk_index=chunk_index,
                    chunk_text=chunk_text,
                    version=document.version,
                    status=document.status,
                )
            )
            chunk_index += 1

        if end >= len(text):
            break

        start = max(0, end - overlap)

    return chunks