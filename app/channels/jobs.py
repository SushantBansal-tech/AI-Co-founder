import asyncio
import logging
import socket
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from app.channels.schemas import IncomingInquiry
from app.channels.service import GraphExecutionError
from app.database import ChannelInboundJob


logger = logging.getLogger(__name__)


class ChannelJobService:
    def __init__(
        self,
        *,
        session_factory,
        ingestion_service,
        worker_id: str | None = None,
        retry_base_seconds: int = 10,
    ) -> None:
        self.session_factory = session_factory
        self.ingestion_service = ingestion_service
        self.worker_id = worker_id or f"{socket.gethostname()}:{uuid4()}"
        self.retry_base_seconds = retry_base_seconds

    async def enqueue(
        self,
        incoming: IncomingInquiry,
        *,
        raw_payload: dict | None = None,
    ) -> tuple[ChannelInboundJob, bool]:
        values = {
            "business_id": incoming.business_id,
            "channel_source_id": incoming.channel_source_id,
            "channel": incoming.channel,
            "provider": incoming.provider,
            "external_event_id": incoming.external_event_id,
            "status": "pending",
            "normalized_payload": incoming.model_dump(mode="json"),
            "raw_payload": raw_payload or {},
            "max_attempts": 8,
        }
        async with self.session_factory() as session:
            job = ChannelInboundJob(**values)
            session.add(job)
            try:
                await session.commit()
                await session.refresh(job)
                return job, False
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(ChannelInboundJob).where(
                        ChannelInboundJob.business_id
                        == incoming.business_id,
                        ChannelInboundJob.channel == incoming.channel,
                        ChannelInboundJob.provider == incoming.provider,
                        ChannelInboundJob.external_event_id
                        == incoming.external_event_id,
                    )
                )
                if existing is None:
                    raise
                return existing, True

    async def recover_stale_jobs(self, *, stale_seconds: int = 300) -> int:
        cutoff = datetime.utcnow() - timedelta(seconds=stale_seconds)
        async with self.session_factory() as session:
            result = await session.execute(
                update(ChannelInboundJob)
                .where(
                    ChannelInboundJob.status == "processing",
                    ChannelInboundJob.locked_at < cutoff,
                )
                .values(
                    status="pending",
                    locked_at=None,
                    locked_by=None,
                    next_attempt_at=datetime.utcnow(),
                )
            )
            await session.commit()
            return int(result.rowcount or 0)

    async def claim_next(self) -> ChannelInboundJob | None:
        now = datetime.utcnow()
        async with self.session_factory() as session:
            dialect = session.bind.dialect.name
            query = (
                select(ChannelInboundJob)
                .where(
                    ChannelInboundJob.status == "pending",
                    or_(
                        ChannelInboundJob.next_attempt_at.is_(None),
                        ChannelInboundJob.next_attempt_at <= now,
                    ),
                )
                .order_by(ChannelInboundJob.created_at, ChannelInboundJob.id)
                .limit(1)
            )
            if dialect == "postgresql":
                query = query.with_for_update(skip_locked=True)

            job = await session.scalar(query)
            if job is None:
                return None

            claimed = await session.execute(
                update(ChannelInboundJob)
                .where(
                    ChannelInboundJob.id == job.id,
                    ChannelInboundJob.status == "pending",
                )
                .values(
                    status="processing",
                    locked_at=now,
                    locked_by=self.worker_id,
                    attempt_count=ChannelInboundJob.attempt_count + 1,
                )
            )
            if not claimed.rowcount:
                await session.rollback()
                return None
            await session.commit()
            return await session.get(ChannelInboundJob, job.id)

    async def process_one(self) -> bool:
        job = await self.claim_next()
        if job is None:
            return False

        try:
            incoming = IncomingInquiry.model_validate(job.normalized_payload)
            response = await self.ingestion_service.ingest(incoming)
            graph_state = response.get("state") or {}
            if graph_state.get("error"):
                failure = graph_state.get("failure") or {}
                raise GraphExecutionError(
                    f"Sales graph failed: {graph_state['error']}",
                    retryable=bool(failure.get("retryable")),
                )
            async with self.session_factory() as session:
                await session.execute(
                    update(ChannelInboundJob)
                    .where(ChannelInboundJob.id == job.id)
                    .values(
                        status="completed",
                        ingestion_id=response.get("ingestion_id"),
                        thread_id=response.get("thread_id"),
                        locked_at=None,
                        locked_by=None,
                        completed_at=datetime.utcnow(),
                        last_error=None,
                    )
                )
                await session.commit()
            return True
        except Exception as exc:
            logger.exception("Channel job %s failed", job.id)
            terminal = (
                (
                    isinstance(exc, GraphExecutionError)
                    and not exc.retryable
                )
                or job.attempt_count >= job.max_attempts
            )
            delay = self.retry_base_seconds * (2 ** max(job.attempt_count - 1, 0))
            async with self.session_factory() as session:
                await session.execute(
                    update(ChannelInboundJob)
                    .where(ChannelInboundJob.id == job.id)
                    .values(
                        status="failed" if terminal else "pending",
                        locked_at=None,
                        locked_by=None,
                        next_attempt_at=(
                            None
                            if terminal
                            else datetime.utcnow() + timedelta(seconds=delay)
                        ),
                        last_error=str(exc)[:4000],
                    )
                )
                await session.commit()
            return True

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        idle_seconds: float = 1.0,
    ) -> None:
        await self.recover_stale_jobs()
        while not stop_event.is_set():
            processed = await self.process_one()
            if not processed:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=idle_seconds,
                    )
                except TimeoutError:
                    pass
