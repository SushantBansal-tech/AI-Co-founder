import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import ProcessedEvent, SessionFactory


class IdempotencyConflict(ValueError):
    pass


class IdempotencyInProgress(ValueError):
    pass


@dataclass
class IdempotencyClaim:
    event_id: str
    cached_response: Optional[dict] = None
    cached_status: Optional[int] = None

    @property
    def is_cached(self) -> bool:
        return self.cached_response is not None


def hash_request(payload: Any) -> str:
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def claim_request(
    *,
    business_id: str,
    endpoint: str,
    idempotency_key: str,
    payload: Any,
    thread_id: Optional[str] = None,
) -> IdempotencyClaim:
    digest = hash_request(payload)
    async with SessionFactory() as session:
        event = ProcessedEvent(
            business_id=business_id,
            endpoint=endpoint,
            idempotency_key=idempotency_key,
            request_hash=digest,
            status="processing",
            thread_id=thread_id,
            locked_at=datetime.utcnow(),
        )
        session.add(event)
        try:
            await session.commit()
            return IdempotencyClaim(event_id=event.id)
        except IntegrityError:
            await session.rollback()
            existing = await session.scalar(
                select(ProcessedEvent).where(
                    ProcessedEvent.business_id == business_id,
                    ProcessedEvent.endpoint == endpoint,
                    ProcessedEvent.idempotency_key == idempotency_key,
                )
            )
            if existing is None:
                raise
            if existing.request_hash != digest:
                raise IdempotencyConflict(
                    "Idempotency-Key was already used with a different request."
                )
            if existing.status == "completed":
                return IdempotencyClaim(
                    event_id=existing.id,
                    cached_response=existing.response_body or {},
                    cached_status=existing.response_status or 200,
                )
            raise IdempotencyInProgress(
                "An identical request with this Idempotency-Key is processing."
            )


async def complete_request(
    event_id: str,
    response_body: dict,
    *,
    response_status: int = 200,
    thread_id: Optional[str] = None,
    interaction_id: Optional[str] = None,
) -> None:
    async with SessionFactory() as session:
        event = await session.get(ProcessedEvent, event_id)
        if event:
            event.status = "completed"
            event.response_status = response_status
            event.response_body = response_body
            event.thread_id = thread_id or event.thread_id
            event.interaction_id = interaction_id
            event.completed_at = datetime.utcnow()
            await session.commit()


async def fail_request(event_id: str, error: Exception) -> None:
    async with SessionFactory() as session:
        event = await session.get(ProcessedEvent, event_id)
        if event:
            event.status = "failed"
            event.error_message = str(error)
            event.completed_at = datetime.utcnow()
            await session.commit()
