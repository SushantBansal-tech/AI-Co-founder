from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from app.channels.outbound import (
    ChannelOutboundDispatcher,
    MessageSendResult,
)
from app.channels.schemas import IncomingInquiry
from app.channels.service import (
    ChannelIngestionService,
    GraphExecutionError,
)
from app.database import (
    BusinessEvent,
    ChannelIngestion,
    ChannelSource,
    Interaction,
    ProcessedEvent,
)
from app.idempotency.service import claim_request


class Snapshot:
    def __init__(self, values):
        self.values = values


class InspectingGraph:
    def __init__(self, result=None, snapshots=None):
        self.result = result or {
            "pipeline_status": "quotation_sent",
            "customer_id": None,
            "lead_id": None,
        }
        self.snapshots = snapshots or {}
        self.invocations = []

    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return Snapshot(self.snapshots.get(thread_id, {}))

    async def ainvoke(self, state, config):
        self.invocations.append((state, config))
        return {**state, **self.result}


def initial_state_factory(**values):
    return values


async def create_source(
    session_factory,
    *,
    channel,
    provider,
    business_id=None,
):
    source = ChannelSource(
        business_id=business_id or f"reliability-{uuid4()}",
        channel=channel,
        provider=provider,
        provider_account_id=(
            str(uuid4()) if channel != "website" else None
        ),
        public_key=f"source-{uuid4()}",
        name="Reliability source",
        configuration={},
    )
    async with session_factory() as session:
        session.add(source)
        await session.commit()
    return source


def incoming(source, *, event_id, sender="buyer@example.com"):
    return IncomingInquiry(
        business_id=source.business_id,
        channel_source_id=source.id,
        channel=source.channel,
        provider=source.provider,
        external_event_id=event_id,
        sender_identifier=sender,
        text="Need 100 MT steel billets",
        received_at=datetime.utcnow(),
    )


@pytest.mark.asyncio
async def test_website_source_is_mapped_to_graph_website(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="website",
        provider="native_form",
    )
    graph = InspectingGraph()
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=graph,
        initial_state_factory=initial_state_factory,
    )
    await service.ingest(
        incoming(source, event_id="website-mapping")
    )
    assert graph.invocations[0][0]["source"] == "website"


@pytest.mark.asyncio
async def test_graph_error_marks_ingestion_failed(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="email",
        provider="imap",
    )
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=InspectingGraph(
            result={"error": "pricing documents unavailable"}
        ),
        initial_state_factory=initial_state_factory,
    )
    with pytest.raises(GraphExecutionError):
        await service.ingest(
            incoming(source, event_id="graph-error")
        )
    async with test_session_factory() as session:
        ingestion = await session.scalar(
            select(ChannelIngestion).where(
                ChannelIngestion.external_event_id == "graph-error"
            )
        )
        assert ingestion.status == "failed"
        assert "pricing documents unavailable" in ingestion.error_message


@pytest.mark.asyncio
async def test_stale_idempotency_claim_is_recovered(
    test_session_factory,
):
    business_id = f"stale-{uuid4()}"
    first = await claim_request(
        business_id=business_id,
        endpoint="/stale-test",
        idempotency_key="stale-request-key",
        payload={"value": 1},
        session_factory=test_session_factory,
    )
    async with test_session_factory() as session:
        await session.execute(
            update(ProcessedEvent)
            .where(ProcessedEvent.id == first.event_id)
            .values(
                locked_at=datetime.utcnow() - timedelta(minutes=30)
            )
        )
        await session.commit()
    recovered = await claim_request(
        business_id=business_id,
        endpoint="/stale-test",
        idempotency_key="stale-request-key",
        payload={"value": 1},
        session_factory=test_session_factory,
        stale_after_seconds=60,
    )
    assert recovered.event_id == first.event_id


@pytest.mark.asyncio
async def test_customer_reply_reuses_quotation_thread(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="email",
        provider="imap",
    )
    thread_id = str(uuid4())
    async with test_session_factory() as session:
        session.add(
            Interaction(
                business_id=source.business_id,
                thread_id=thread_id,
                direction="incoming",
                channel="email",
                message_type="inquiry",
                sender="buyer@example.com",
                content="Original inquiry",
                status="received",
            )
        )
        await session.commit()
    graph = InspectingGraph(
        snapshots={
            thread_id: {
                "business_id": source.business_id,
                "thread_id": thread_id,
                "pipeline_status": "quotation_sent",
                "final_draft_json": "{}",
                "customer_id": None,
                "lead_id": None,
            }
        }
    )
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=graph,
        initial_state_factory=initial_state_factory,
    )
    response = await service.ingest(
        incoming(
            source,
            event_id="email-reply",
            sender="buyer@example.com",
        )
    )
    invoked_state, config = graph.invocations[0]
    assert response["thread_id"] == thread_id
    assert invoked_state["trigger"] == "customer_reply"
    assert config["configurable"]["thread_id"] == thread_id


@pytest.mark.asyncio
async def test_email_reply_resumes_website_thread_cross_channel(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="email",
        provider="imap",
    )
    thread_id = str(uuid4())
    async with test_session_factory() as session:
        session.add(
            Interaction(
                business_id=source.business_id,
                thread_id=thread_id,
                direction="outgoing",
                channel="email",
                message_type="quotation",
                external_message_id="<quote-cross-channel@example.com>",
                recipient="buyer@example.com",
                content="Quotation",
                status="sent",
            )
        )
        await session.commit()
    graph = InspectingGraph(
        snapshots={
            thread_id: {
                "pipeline_status": "awaiting_customer_reply",
                "final_draft_json": "{}",
            }
        }
    )
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=graph,
        initial_state_factory=initial_state_factory,
    )
    reply = incoming(source, event_id="email-cross-channel")
    reply.metadata = {
        "message_id": "<reply-cross-channel@example.com>",
        "in_reply_to": "<quote-cross-channel@example.com>",
        "references": ["<quote-cross-channel@example.com>"],
    }
    response = await service.ingest(reply)
    assert response["thread_id"] == thread_id
    assert graph.invocations[0][0]["trigger"] == "customer_reply"


@pytest.mark.asyncio
async def test_reply_headers_disambiguate_multiple_open_threads(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="email",
        provider="imap",
    )
    selected_thread = str(uuid4())
    other_thread = str(uuid4())
    async with test_session_factory() as session:
        session.add_all(
            [
                Interaction(
                    business_id=source.business_id,
                    thread_id=selected_thread,
                    direction="outgoing",
                    channel="email",
                    message_type="quotation",
                    external_message_id="<selected-quote@example.com>",
                    recipient="buyer@example.com",
                    content="Selected quotation",
                    status="sent",
                ),
                Interaction(
                    business_id=source.business_id,
                    thread_id=other_thread,
                    direction="outgoing",
                    channel="email",
                    message_type="quotation",
                    external_message_id="<other-quote@example.com>",
                    recipient="buyer@example.com",
                    content="Other quotation",
                    status="sent",
                ),
            ]
        )
        await session.commit()
    graph = InspectingGraph(
        snapshots={
            selected_thread: {
                "pipeline_status": "awaiting_customer_reply",
                "final_draft_json": "{}",
            },
            other_thread: {
                "pipeline_status": "awaiting_purchase_order",
                "final_draft_json": "{}",
            },
        }
    )
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=graph,
        initial_state_factory=initial_state_factory,
    )
    reply = incoming(source, event_id="email-header-match")
    reply.metadata = {
        "message_id": "<reply-header-match@example.com>",
        "in_reply_to": "<selected-quote@example.com>",
        "references": ["<selected-quote@example.com>"],
    }
    response = await service.ingest(reply)
    assert response["thread_id"] == selected_thread


@pytest.mark.asyncio
async def test_ambiguous_sender_is_not_attached_to_random_thread(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="email",
        provider="imap",
    )
    threads = [str(uuid4()), str(uuid4())]
    async with test_session_factory() as session:
        session.add_all(
            [
                Interaction(
                    business_id=source.business_id,
                    thread_id=thread_id,
                    direction="outgoing",
                    channel="email",
                    message_type="quotation",
                    recipient="buyer@example.com",
                    content="Quotation",
                    status="sent",
                )
                for thread_id in threads
            ]
        )
        await session.commit()
    graph = InspectingGraph(
        snapshots={
            thread_id: {
                "pipeline_status": "awaiting_customer_reply",
                "final_draft_json": "{}",
            }
            for thread_id in threads
        }
    )
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=graph,
        initial_state_factory=initial_state_factory,
    )
    with pytest.raises(GraphExecutionError, match="Ambiguous customer reply"):
        await service.ingest(
            incoming(source, event_id="ambiguous-email-reply")
        )


@pytest.mark.asyncio
async def test_initial_interaction_and_events_are_backfilled(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="website",
        provider="native_form",
    )
    customer_id = str(uuid4())
    lead_id = str(uuid4())
    service = ChannelIngestionService(
        session_factory=test_session_factory,
        sales_graph=InspectingGraph(
            result={
                "pipeline_status": "awaiting_approval:qualification",
                "customer_id": customer_id,
                "lead_id": lead_id,
            }
        ),
        initial_state_factory=initial_state_factory,
    )
    response = await service.ingest(
        incoming(source, event_id="backfill-identifiers")
    )

    async with test_session_factory() as session:
        interaction = await session.get(
            Interaction,
            response["interaction_id"],
        )
        events = (
            await session.execute(
                select(BusinessEvent).where(
                    BusinessEvent.thread_id == response["thread_id"]
                )
            )
        ).scalars().all()

    assert interaction.customer_id == customer_id
    assert interaction.lead_id == lead_id
    assert events
    assert all(event.customer_id == customer_id for event in events)
    assert all(event.lead_id == lead_id for event in events)


class ConfirmingEmailSender:
    async def send(self, **kwargs):
        return MessageSendResult("<provider-id@example.com>", "sent")


@pytest.mark.asyncio
async def test_outbound_dispatch_requires_provider_confirmation(
    test_session_factory,
):
    source = await create_source(
        test_session_factory,
        channel="email",
        provider="imap",
    )
    dispatcher = ChannelOutboundDispatcher(
        session_factory=test_session_factory,
        email_sender=ConfirmingEmailSender(),
    )
    result = await dispatcher.send(
        business_id=source.business_id,
        channel="email",
        recipient="buyer@example.com",
        subject="Quotation",
        text="Quotation body",
    )
    assert result.confirmed
    assert result.provider_message_id == "<provider-id@example.com>"
