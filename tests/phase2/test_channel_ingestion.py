import asyncio
from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from app.channels.repository import get_source_for_business
from app.channels.schemas import IncomingInquiry
from app.channels.service import ChannelIngestionService
from app.database import (
    BusinessEvent,
    ChannelIngestion,
    ChannelSource,
    Interaction,
    ProcessedEvent,
)
from app.idempotency.service import IdempotencyInProgress


class FakeGraph:
    def __init__(self, delay: float = 0):
        self.calls = 0
        self.delay = delay

    async def ainvoke(self, state, config):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {
            **state,
            "lead_id": None,
            "customer_id": None,
            "pipeline_status": "test_complete",
        }


def initial_state_factory(**values):
    return values


def incoming(source, submission_id, message="Need 100 MT steel billets"):
    return IncomingInquiry(
        business_id=source.business_id,
        channel_source_id=source.id,
        channel="website",
        provider=source.provider,
        external_event_id=submission_id,
        sender_identifier="buyer@example.com",
        sender_name="Test Buyer",
        subject="Website inquiry",
        text=message,
        received_at=datetime.utcnow(),
        metadata={"raw_payload": {"message": message}},
    )


async def create_source(session_factory, business_id):
    source = ChannelSource(
        business_id=business_id,
        channel="website",
        provider="native_form",
        public_key=f"form-{uuid4()}",
        name="Test form",
        active=True,
        configuration={"max_submissions_per_minute": 100},
    )
    async with session_factory() as session:
        session.add(source)
        await session.commit()
    return source


async def cleanup_business(session_factory, business_id):
    async with session_factory() as session:
        await session.execute(
            delete(ChannelIngestion).where(
                ChannelIngestion.business_id == business_id
            )
        )
        await session.execute(
            delete(BusinessEvent).where(
                BusinessEvent.business_id == business_id
            )
        )
        await session.execute(
            delete(Interaction).where(
                Interaction.business_id == business_id
            )
        )
        await session.execute(
            delete(ProcessedEvent).where(
                ProcessedEvent.business_id == business_id
            )
        )
        await session.execute(
            delete(ChannelSource).where(
                ChannelSource.business_id == business_id
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_tenant_isolation_uses_source_business(test_session_factory):
    business_a = f"tenant-a-{uuid4()}"
    business_b = f"tenant-b-{uuid4()}"
    source = await create_source(test_session_factory, business_a)
    try:
        async with test_session_factory() as session:
            assert await get_source_for_business(
                session,
                source_id=source.id,
                business_id=business_a,
            )
            assert await get_source_for_business(
                session,
                source_id=source.id,
                business_id=business_b,
            ) is None
    finally:
        await cleanup_business(test_session_factory, business_a)


@pytest.mark.asyncio
async def test_same_submission_returns_cached_result_once(
    test_session_factory,
):
    business_id = f"idempotency-{uuid4()}"
    source = await create_source(test_session_factory, business_id)
    graph = FakeGraph()
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=graph,
        initial_state_factory=initial_state_factory,
    )
    try:
        first = await service.ingest(incoming(source, "submission-0001"))
        second = await service.ingest(incoming(source, "submission-0001"))
        assert first == second
        assert graph.calls == 1

        async with test_session_factory() as session:
            assert await session.scalar(
                select(func.count(ChannelIngestion.id)).where(
                    ChannelIngestion.business_id == business_id
                )
            ) == 1
            assert await session.scalar(
                select(func.count(Interaction.id)).where(
                    Interaction.business_id == business_id
                )
            ) == 1
    finally:
        await cleanup_business(test_session_factory, business_id)


@pytest.mark.asyncio
async def test_same_submission_with_different_content_conflicts(
    test_session_factory,
):
    business_id = f"conflict-{uuid4()}"
    source = await create_source(test_session_factory, business_id)
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=FakeGraph(),
        initial_state_factory=initial_state_factory,
    )
    try:
        await service.ingest(incoming(source, "submission-0002", "First"))
        with pytest.raises(ValueError):
            await service.ingest(
                incoming(source, "submission-0002", "Changed")
            )
    finally:
        await cleanup_business(test_session_factory, business_id)


@pytest.mark.asyncio
async def test_concurrent_duplicates_execute_graph_once(
    test_session_factory,
):
    business_id = f"concurrency-{uuid4()}"
    source = await create_source(test_session_factory, business_id)
    graph = FakeGraph(delay=0.2)
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=graph,
        initial_state_factory=initial_state_factory,
    )

    async def submit():
        try:
            return await service.ingest(
                incoming(source, "submission-concurrent")
            )
        except IdempotencyInProgress:
            return None

    try:
        results = await asyncio.gather(*(submit() for _ in range(10)))
        assert len([result for result in results if result]) == 1
        assert graph.calls == 1
        async with test_session_factory() as session:
            assert await session.scalar(
                select(func.count(ChannelIngestion.id)).where(
                    ChannelIngestion.business_id == business_id
                )
            ) == 1
    finally:
        await cleanup_business(test_session_factory, business_id)


@pytest.mark.asyncio
async def test_restart_reuses_persisted_response(test_session_factory):
    business_id = f"restart-{uuid4()}"
    source = await create_source(test_session_factory, business_id)
    first_graph = FakeGraph()
    first_service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=first_graph,
        initial_state_factory=initial_state_factory,
    )
    try:
        first = await first_service.ingest(
            incoming(source, "submission-restart")
        )

        second_graph = FakeGraph()
        second_service = ChannelIngestionService(
            session_factory=test_session_factory,
            sales_graph=second_graph,
            initial_state_factory=initial_state_factory,
        )
        second = await second_service.ingest(
            incoming(source, "submission-restart")
        )

        assert second == first
        assert second_graph.calls == 0
        assert second["thread_id"] == first["thread_id"]
    finally:
        await cleanup_business(test_session_factory, business_id)
