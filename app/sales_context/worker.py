import asyncio
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update

from app.database import Customer, CustomerNote, MemoryOutbox
from app.database.models.memory import utc_now


class CustomerMemoryService:
    def __init__(self, session_factory) -> None:
        self.session_factory = session_factory

    async def create_note(self, *, business_id: str, customer_id: str, content: str,
                          content_type: str, thread_id: str | None = None,
                          interaction_id: str | None = None, created_by: str = "api",
                          request_event_id: str | None = None) -> tuple[str, str]:
        async with self.session_factory() as session:
            customer = await session.scalar(select(Customer.id).where(
                Customer.id == customer_id, Customer.business_id == business_id,
            ))
            if customer is None:
                raise ValueError("Customer not found.")
            if request_event_id:
                existing = await session.scalar(select(CustomerNote).where(
                    CustomerNote.business_id == business_id,
                    CustomerNote.request_event_id == request_event_id,
                ))
                if existing is not None:
                    existing_job = await session.scalar(select(MemoryOutbox).where(
                        MemoryOutbox.business_id == business_id,
                        MemoryOutbox.source_type == "customer_note",
                        MemoryOutbox.source_id == existing.id,
                    ))
                    return existing.id, existing_job.id
            note = CustomerNote(
                business_id=business_id, customer_id=customer_id, content=content,
                content_type=content_type, thread_id=thread_id,
                interaction_id=interaction_id, created_by=created_by,
                request_event_id=request_event_id,
            )
            session.add(note)
            await session.flush()
            job = MemoryOutbox(
                business_id=business_id, customer_id=customer_id,
                source_type="customer_note", source_id=note.id,
                memory_type=content_type, content=content,
                thread_id=thread_id, interaction_id=interaction_id,
            )
            session.add(job)
            await session.commit()
            return note.id, job.id


class MemoryOutboxWorker:
    def __init__(self, *, session_factory, embedding_service, vector_store,
                 worker_id: str | None = None, max_attempts: int = 5) -> None:
        self.session_factory = session_factory
        self.embedding_service = embedding_service
        self.vector_store = vector_store
        self.worker_id = worker_id or f"memory-{uuid4()}"
        self.max_attempts = max_attempts

    async def recover_stale(self, stale_seconds: int = 300) -> int:
        cutoff = utc_now() - timedelta(seconds=stale_seconds)
        async with self.session_factory() as session:
            result = await session.execute(update(MemoryOutbox).where(
                MemoryOutbox.status == "processing", MemoryOutbox.locked_at < cutoff,
            ).values(status="retry", locked_at=None, locked_by=None, next_attempt_at=utc_now()))
            await session.commit()
            return int(result.rowcount or 0)

    async def claim(self) -> MemoryOutbox | None:
        now = utc_now()
        async with self.session_factory() as session:
            job = await session.scalar(select(MemoryOutbox).where(
                MemoryOutbox.status.in_(("pending", "retry")),
                MemoryOutbox.next_attempt_at <= now,
            ).order_by(MemoryOutbox.created_at).with_for_update(skip_locked=True).limit(1))
            if job is None:
                return None
            job.status = "processing"
            job.locked_at = now
            job.locked_by = self.worker_id
            job.attempt_count += 1
            await session.commit()
            return job

    async def process_one(self) -> bool:
        job = await self.claim()
        if job is None:
            return False
        try:
            vector = await self.embedding_service.embed_query(job.content)
            self.vector_store.upsert_customer_note(
                vector=vector, business_id=job.business_id,
                customer_id=job.customer_id, content=job.content,
                content_type=job.memory_type, thread_id=job.thread_id,
                interaction_id=job.interaction_id,
                occurred_at=job.created_at.isoformat(), note_id=job.id,
                source_type=job.source_type, source_id=job.source_id,
            )
            async with self.session_factory() as session:
                await session.execute(update(MemoryOutbox).where(
                    MemoryOutbox.id == job.id, MemoryOutbox.locked_by == self.worker_id,
                ).values(status="completed", completed_at=utc_now(), locked_at=None,
                         locked_by=None, last_error=None, updated_at=utc_now()))
                await session.commit()
        except Exception as exc:
            status = "dead" if job.attempt_count >= self.max_attempts else "retry"
            delay = min(30 * (2 ** max(job.attempt_count - 1, 0)), 3600)
            async with self.session_factory() as session:
                await session.execute(update(MemoryOutbox).where(
                    MemoryOutbox.id == job.id, MemoryOutbox.locked_by == self.worker_id,
                ).values(status=status, next_attempt_at=utc_now() + timedelta(seconds=delay),
                         locked_at=None, locked_by=None, last_error=str(exc), updated_at=utc_now()))
                await session.commit()
        return True

    async def run(self, stop_event: asyncio.Event, idle_seconds: float = 2,
                  stale_seconds: int = 300) -> None:
        await self.recover_stale(stale_seconds)
        while not stop_event.is_set():
            worked = await self.process_one()
            if not worked:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=idle_seconds)
                except TimeoutError:
                    pass
