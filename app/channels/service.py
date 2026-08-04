from datetime import datetime
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, update

from app.channels.schemas import IncomingInquiry
from app.database import BusinessEvent, ChannelIngestion, Interaction
from app.events.interactions import record_interaction
from app.events.service import record_business_event
from app.idempotency.service import (
    claim_request,
    complete_request,
    fail_request,
)


class GraphExecutionError(RuntimeError):
    pass


class ChannelIngestionService:
    def __init__(
        self,
        *,
        session_factory,
        sales_graph,
        initial_state_factory,
    ) -> None:
        self.session_factory = session_factory
        self.sales_graph = sales_graph
        self.initial_state_factory = initial_state_factory

    @staticmethod
    def idempotency_key(incoming: IncomingInquiry) -> str:
        return (
            f"{incoming.channel}:{incoming.provider}:"
            f"{incoming.channel_source_id}:{incoming.external_event_id}"
        )

    @staticmethod
    def _graph_source(channel: str) -> str:
        return channel

    @staticmethod
    def _outbound_channel(incoming: IncomingInquiry) -> str:
        if incoming.channel in {"email", "whatsapp"}:
            return incoming.channel
        return (
            "email"
            if "@" in incoming.sender_identifier
            else "whatsapp"
        )

    async def _continuation_state(
        self,
        incoming: IncomingInquiry,
    ) -> tuple[str | None, dict]:
        async with self.session_factory() as session:
            candidates = (
                await session.execute(
                    select(Interaction)
                    .where(
                        Interaction.business_id == incoming.business_id,
                        Interaction.channel == incoming.channel,
                        Interaction.sender == incoming.sender_identifier,
                        Interaction.thread_id.is_not(None),
                    )
                    .order_by(Interaction.occurred_at.desc())
                    .limit(5)
                )
            ).scalars().all()

        resumable_statuses = {
            "quotation_sent",
            "followup_sent",
            "negotiating",
            "objection_addressed",
            "rejection_sent",
            "awaiting_revised_po",
        }
        for interaction in candidates:
            config = {
                "configurable": {
                    "thread_id": interaction.thread_id,
                }
            }
            snapshot = await self.sales_graph.aget_state(config)
            state = dict(snapshot.values or {})
            status = state.get("pipeline_status", "")
            if (
                state.get("final_draft_json")
                and (
                    status in resumable_statuses
                    or status.startswith("awaiting_approval:")
                )
            ):
                return interaction.thread_id, state
        return None, {}

    async def ingest(self, incoming: IncomingInquiry) -> dict:
        idempotency_key = self.idempotency_key(incoming)
        claim = await claim_request(
            business_id=incoming.business_id,
            endpoint="/channels/ingest",
            idempotency_key=idempotency_key,
            # received_at is assigned by an adapter and can differ when the
            # same provider delivery is retried. Provider identity and
            # normalized message content define request equality.
            payload=incoming.model_dump(
                mode="json",
                exclude={"received_at"},
            ),
            session_factory=self.session_factory,
        )
        if claim.is_cached:
            return claim.cached_response

        ingestion_id = None
        interaction_id = None
        continuation_thread_id, previous_state = (
            await self._continuation_state(incoming)
        )
        thread_id = continuation_thread_id or str(uuid4())
        is_continuation = continuation_thread_id is not None

        try:
            async with self.session_factory() as session:
                existing_ingestion = await session.scalar(
                    select(ChannelIngestion).where(
                        ChannelIngestion.business_id
                        == incoming.business_id,
                        ChannelIngestion.channel == incoming.channel,
                        ChannelIngestion.provider == incoming.provider,
                        ChannelIngestion.external_event_id
                        == incoming.external_event_id,
                    )
                )
                if existing_ingestion is not None:
                    raise GraphExecutionError(
                        "This provider event previously failed and "
                        "requires explicit operator retry or replay."
                    )
                ingestion = ChannelIngestion(
                    business_id=incoming.business_id,
                    channel_source_id=incoming.channel_source_id,
                    channel=incoming.channel,
                    provider=incoming.provider,
                    external_event_id=incoming.external_event_id,
                    status="processing",
                    raw_payload=incoming.metadata.get("raw_payload", {}),
                    normalized_payload=incoming.model_dump(mode="json"),
                )
                session.add(ingestion)
                await session.flush()

                interaction = await record_interaction(
                    session,
                    business_id=incoming.business_id,
                    thread_id=thread_id,
                    direction="incoming",
                    channel=incoming.channel,
                    customer_id=previous_state.get("customer_id"),
                    lead_id=previous_state.get("lead_id"),
                    message_type=(
                        "customer_reply" if is_continuation else "inquiry"
                    ),
                    external_message_id=idempotency_key,
                    sender=incoming.sender_identifier,
                    subject=incoming.subject,
                    content=incoming.text,
                    status="received",
                    metadata={
                        "provider": incoming.provider,
                        "channel_source_id": incoming.channel_source_id,
                        "sender_name": incoming.sender_name,
                        "attachments": [
                            attachment.model_dump(mode="json")
                            for attachment in incoming.attachments
                        ],
                    },
                )
                ingestion.interaction_id = interaction.id
                ingestion.thread_id = thread_id
                await record_business_event(
                    session,
                    business_id=incoming.business_id,
                    thread_id=thread_id,
                    customer_id=previous_state.get("customer_id"),
                    lead_id=previous_state.get("lead_id"),
                    event_type=(
                        "customer_reply.received"
                        if is_continuation
                        else "inquiry.received"
                    ),
                    source="channel",
                    actor_type="customer",
                    actor_id=incoming.sender_identifier,
                    entity_type="interaction",
                    entity_id=interaction.id,
                    data={
                        "channel": incoming.channel,
                        "provider": incoming.provider,
                        "channel_source_id": incoming.channel_source_id,
                    },
                )
                if is_continuation:
                    from app.followups.service import (
                        cancel_open_followup_jobs,
                    )
                    await cancel_open_followup_jobs(
                        session,
                        business_id=incoming.business_id,
                        thread_id=thread_id,
                        reason="Customer replied.",
                    )
                await session.commit()
                ingestion_id = ingestion.id
                interaction_id = interaction.id

            if is_continuation:
                invocation_state = {
                    "business_id": incoming.business_id,
                    "trigger": "customer_reply",
                    "customer_reply_text": incoming.text,
                    "outbound_channel": self._outbound_channel(incoming),
                    "outbound_recipient": incoming.sender_identifier,
                    "error": None,
                }
            else:
                invocation_state = self.initial_state_factory(
                    trigger="inquiry",
                    business_id=incoming.business_id,
                    source=self._graph_source(incoming.channel),
                    raw_text=incoming.text,
                    sender_identifier=incoming.sender_identifier,
                    outbound_channel=self._outbound_channel(incoming),
                    outbound_recipient=incoming.sender_identifier,
                )
                invocation_state["thread_id"] = thread_id

            result = await self.sales_graph.ainvoke(
                invocation_state,
                config={
                    "configurable": {
                        "thread_id": thread_id,
                    }
                },
            )
            if result.get("error"):
                raise GraphExecutionError(result["error"])

            async with self.session_factory() as session:
                await session.execute(
                    update(ChannelIngestion)
                    .where(ChannelIngestion.id == ingestion_id)
                    .values(
                        status="completed",
                        processed_at=datetime.utcnow(),
                    )
                )
                await session.execute(
                    update(Interaction)
                    .where(Interaction.id == interaction_id)
                    .values(
                        customer_id=result.get("customer_id"),
                        lead_id=result.get("lead_id"),
                    )
                )
                await session.execute(
                    update(BusinessEvent)
                    .where(
                        BusinessEvent.business_id == incoming.business_id,
                        BusinessEvent.thread_id == thread_id,
                        BusinessEvent.customer_id.is_(None),
                    )
                    .values(
                        customer_id=result.get("customer_id"),
                        lead_id=result.get("lead_id"),
                    )
                )
                await session.commit()

            response = {
                "ingestion_id": ingestion_id,
                "interaction_id": interaction_id,
                "thread_id": thread_id,
                "state": result,
            }
            encoded_response = jsonable_encoder(response)
            await complete_request(
                claim.event_id,
                encoded_response,
                response_status=202,
                thread_id=thread_id,
                interaction_id=interaction_id,
                session_factory=self.session_factory,
            )
            return encoded_response
        except Exception as exc:
            if ingestion_id:
                async with self.session_factory() as session:
                    await session.execute(
                        update(ChannelIngestion)
                        .where(ChannelIngestion.id == ingestion_id)
                        .values(
                            status="failed",
                            error_message=str(exc),
                            processed_at=datetime.utcnow(),
                        )
                    )
                    await session.commit()
            await fail_request(
                claim.event_id,
                exc,
                session_factory=self.session_factory,
            )
            raise
