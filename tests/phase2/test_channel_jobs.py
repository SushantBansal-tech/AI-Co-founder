import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from app.channels.jobs import ChannelJobService
from app.channels.schemas import IncomingInquiry
from app.database import ChannelInboundJob, ChannelSource


class CountingIngestionService:
    def __init__(self):
        self.calls = 0

    async def ingest(self, incoming):
        self.calls += 1
        await asyncio.sleep(0.02)
        return {
            "ingestion_id": None,
            "thread_id": f"thread-{incoming.external_event_id}",
        }


def incoming(source, event_id):
    return IncomingInquiry(
        business_id=source.business_id,
        channel_source_id=source.id,
        channel="whatsapp",
        provider="meta_cloud",
        external_event_id=event_id,
        sender_identifier="919999999999",
        text="Need 100 MT steel",
        received_at=datetime.utcnow(),
    )


async def source(session_factory):
    record = ChannelSource(
        business_id=f"jobs-{uuid4()}",
        channel="whatsapp",
        provider="meta_cloud",
        provider_account_id=f"phone-{uuid4()}",
        public_key=f"wa-{uuid4()}",
        name="Worker test",
    )
    async with session_factory() as session:
        session.add(record)
        await session.commit()
    return record


@pytest.mark.asyncio
async def test_concurrent_enqueue_and_workers_process_event_once(
    test_session_factory,
):
    channel_source = await source(test_session_factory)
    ingestion = CountingIngestionService()
    workers = [
        ChannelJobService(
            session_factory=test_session_factory,
            ingestion_service=ingestion,
            worker_id=f"worker-{number}",
        )
        for number in range(5)
    ]

    results = await asyncio.gather(
        *[
            workers[0].enqueue(
                incoming(channel_source, "wamid.concurrent")
            )
            for _ in range(10)
        ]
    )
    assert len({job.id for job, _ in results}) == 1
    assert sum(not duplicate for _, duplicate in results) == 1

    await asyncio.gather(*(worker.process_one() for worker in workers))
    assert ingestion.calls == 1
    async with test_session_factory() as session:
        assert await session.scalar(
            select(func.count(ChannelInboundJob.id))
        ) == 1
        job = await session.scalar(select(ChannelInboundJob))
        assert job.status == "completed"


@pytest.mark.asyncio
async def test_restart_recovers_stale_processing_job(test_session_factory):
    channel_source = await source(test_session_factory)
    first = ChannelJobService(
        session_factory=test_session_factory,
        ingestion_service=CountingIngestionService(),
        worker_id="old-worker",
    )
    job, _ = await first.enqueue(
        incoming(channel_source, "wamid.restart")
    )
    async with test_session_factory() as session:
        await session.execute(
            update(ChannelInboundJob)
            .where(ChannelInboundJob.id == job.id)
            .values(
                status="processing",
                locked_by="dead-worker",
                locked_at=datetime.utcnow() - timedelta(minutes=10),
            )
        )
        await session.commit()

    ingestion = CountingIngestionService()
    restarted = ChannelJobService(
        session_factory=test_session_factory,
        ingestion_service=ingestion,
        worker_id="new-worker",
    )
    assert await restarted.recover_stale_jobs(stale_seconds=60) == 1
    assert await restarted.process_one() is True
    assert ingestion.calls == 1


@pytest.mark.asyncio
async def test_jobs_remain_tenant_scoped(test_session_factory):
    source_a = await source(test_session_factory)
    source_b = await source(test_session_factory)
    service = ChannelJobService(
        session_factory=test_session_factory,
        ingestion_service=CountingIngestionService(),
    )
    await service.enqueue(incoming(source_a, "same-provider-id"))
    await service.enqueue(incoming(source_b, "same-provider-id"))
    async with test_session_factory() as session:
        assert await session.scalar(
            select(func.count(ChannelInboundJob.id)).where(
                ChannelInboundJob.business_id == source_a.business_id
            )
        ) == 1
        assert await session.scalar(
            select(func.count(ChannelInboundJob.id)).where(
                ChannelInboundJob.business_id == source_b.business_id
            )
        ) == 1
