from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.database import Customer, CustomerNote, MemoryOutbox
from app.sales_context import CustomerMemoryService, MemoryOutboxWorker, SalesContextService


class FakeEmbeddings:
    async def embed_query(self, text):
        return [0.1, 0.2, 0.3]


class FakeVectorStore:
    def __init__(self, *, fail_search=False):
        self.fail_search = fail_search
        self.search_calls = []
        self.upserts = []

    def search_customer_memory(self, **kwargs):
        self.search_calls.append(kwargs)
        if self.fail_search:
            raise RuntimeError("qdrant offline")
        return [{
            "memory_id": "memory-1",
            "content": "Customer prefers 45-day credit terms.",
            "content_type": "relationship_note",
            "score": 0.91,
            "occurred_at": None,
            "thread_id": None,
        }]

    def upsert_customer_note(self, **kwargs):
        self.upserts.append(kwargs)
        return kwargs["note_id"]


async def create_customer(session_factory, business_id="tenant-a"):
    customer = Customer(
        business_id=business_id,
        company_name="Memory Test Steel",
        customer_type="existing",
    )
    async with session_factory() as session:
        session.add(customer)
        await session.commit()
    return customer


@pytest.mark.asyncio
async def test_note_is_durable_before_qdrant_projection(test_session_factory):
    customer = await create_customer(test_session_factory)
    service = CustomerMemoryService(test_session_factory)

    note_id, outbox_id = await service.create_note(
        business_id=customer.business_id,
        customer_id=customer.id,
        content="Negotiated 12% discount last time.",
        content_type="negotiation_summary",
    )

    async with test_session_factory() as session:
        assert await session.get(CustomerNote, note_id) is not None
        job = await session.get(MemoryOutbox, outbox_id)
        assert job.status == "pending"


@pytest.mark.asyncio
async def test_note_retry_reuses_same_postgres_note(test_session_factory):
    customer = await create_customer(test_session_factory)
    service = CustomerMemoryService(test_session_factory)
    request_event_id = str(uuid4())
    first = await service.create_note(
        business_id=customer.business_id, customer_id=customer.id,
        content="Prefers email.", content_type="relationship_note",
        request_event_id=request_event_id,
    )
    second = await service.create_note(
        business_id=customer.business_id, customer_id=customer.id,
        content="Prefers email.", content_type="relationship_note",
        request_event_id=request_event_id,
    )
    assert second == first
    async with test_session_factory() as session:
        count = await session.scalar(select(func.count(CustomerNote.id)))
        assert count == 1


@pytest.mark.asyncio
async def test_worker_projects_once_with_deterministic_id(test_session_factory):
    customer = await create_customer(test_session_factory)
    service = CustomerMemoryService(test_session_factory)
    _, outbox_id = await service.create_note(
        business_id=customer.business_id,
        customer_id=customer.id,
        content="Prefers delivery before the 15th.",
        content_type="relationship_note",
    )
    store = FakeVectorStore()
    worker = MemoryOutboxWorker(
        session_factory=test_session_factory,
        embedding_service=FakeEmbeddings(),
        vector_store=store,
    )

    assert await worker.process_one() is True
    assert await worker.process_one() is False
    assert store.upserts[0]["note_id"] == outbox_id
    async with test_session_factory() as session:
        assert (await session.get(MemoryOutbox, outbox_id)).status == "completed"


@pytest.mark.asyncio
async def test_sales_context_is_tenant_scoped(test_session_factory):
    customer = await create_customer(test_session_factory, "tenant-a")
    store = FakeVectorStore()
    service = SalesContextService(
        session_factory=test_session_factory,
        embedding_service=FakeEmbeddings(),
        vector_store=store,
    )
    context = await service.get_context(
        business_id="tenant-a", customer_id=customer.id,
        agent_name="customer_qualification", state={},
    )
    assert context.semantic_memories[0].score == 0.91
    assert store.search_calls[0]["business_id"] == "tenant-a"
    assert store.search_calls[0]["customer_id"] == customer.id

    with pytest.raises(ValueError, match="Customer not found"):
        await service.get_context(
            business_id="tenant-b", customer_id=customer.id,
            agent_name="customer_qualification", state={},
        )


@pytest.mark.asyncio
async def test_postgres_context_survives_qdrant_outage(test_session_factory):
    customer = await create_customer(test_session_factory)
    service = SalesContextService(
        session_factory=test_session_factory,
        embedding_service=FakeEmbeddings(),
        vector_store=FakeVectorStore(fail_search=True),
    )
    context = await service.get_context(
        business_id=customer.business_id,
        customer_id=customer.id,
        agent_name="customer_qualification",
        state={},
    )
    assert context.customer_360["customer"]["id"] == customer.id
    assert context.semantic_memory_available is False
    assert "qdrant offline" in context.warnings[0]
