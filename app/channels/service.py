from datetime import datetime
from uuid import uuid4

from fastapi.encoders import jsonable_encoder
from sqlalchemy import or_, select, update

from app.channels.schemas import IncomingInquiry
from app.database import (
    BusinessEvent,
    ChannelConversation,
    ChannelIngestion,
    Interaction,
)
from app.events.interactions import record_interaction
from app.events.service import record_business_event
from app.idempotency.service import (
    claim_request,
    complete_request,
    fail_request,
)


class GraphExecutionError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ChannelIngestionService:
    RESUMABLE_STATUSES = {
        "awaiting_customer_reply",
        "awaiting_purchase_order",
        "awaiting_corrected_po",
        "awaiting_approval",
        # Legacy statuses retained for checkpoints created before the
        # explicit business-status migration.
        "quotation_sent",
        "followup_sent",
        "negotiating",
        "objection_addressed",
        "rejection_sent",
        "awaiting_revised_po",
    }
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

    @staticmethod
    def _message_references(incoming: IncomingInquiry) -> list[str]:
        values = [
            incoming.metadata.get("in_reply_to"),
            *(incoming.metadata.get("references") or []),
        ]
        return list(dict.fromkeys(str(value).strip() for value in values if value))

    @classmethod
    def _is_resumable(cls, state: dict) -> bool:
        status = str(state.get("pipeline_status") or "")
        return bool(
            state.get("final_draft_json")
            and (
                status in cls.RESUMABLE_STATUSES
                or status.startswith("awaiting_approval:")
            )
        )

    async def _load_resumable_state(
        self,
        thread_id: str,
    ) -> dict | None:
        snapshot = await self.sales_graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        state = dict(snapshot.values or {})
        return state if self._is_resumable(state) else None

    async def _select_unique_resumable_thread(
        self,
        thread_ids: list[str],
        *,
        sender: str,
    ) -> tuple[str | None, dict]:
        matches: list[tuple[str, dict]] = []
        for thread_id in dict.fromkeys(thread_ids):
            state = await self._load_resumable_state(thread_id)
            if state is not None:
                matches.append((thread_id, state))
        if len(matches) > 1:
            raise GraphExecutionError(
                "Ambiguous customer reply: multiple open sales conversations "
                f"exist for {sender}. Match by In-Reply-To/References or route "
                "to operator review."
            )
        return matches[0] if matches else (None, {})

    async def _continuation_state(
        self,
        incoming: IncomingInquiry,
    ) -> tuple[str | None, dict]:
        references = self._message_references(incoming)
        participant = incoming.sender_identifier.strip().lower()
        async with self.session_factory() as session:
            # RFC message headers are authoritative. They disambiguate two
            # simultaneous quotations sent to the same customer mailbox.
            if references:
                exact_thread = await session.scalar(
                    select(Interaction.thread_id)
                    .where(
                        Interaction.business_id == incoming.business_id,
                        Interaction.external_message_id.in_(references),
                        Interaction.thread_id.is_not(None),
                    )
                    .order_by(Interaction.occurred_at.desc())
                    .limit(1)
                )
                if exact_thread:
                    state = await self._load_resumable_state(exact_thread)
                    if state is not None:
                        return exact_thread, state

                exact_thread = await session.scalar(
                    select(ChannelConversation.thread_id)
                    .where(
                        ChannelConversation.business_id == incoming.business_id,
                        ChannelConversation.channel == incoming.channel,
                        or_(
                            ChannelConversation.external_conversation_id.in_(references),
                            ChannelConversation.root_message_id.in_(references),
                            ChannelConversation.latest_message_id.in_(references),
                        ),
                    )
                    .limit(1)
                )
                if exact_thread:
                    state = await self._load_resumable_state(exact_thread)
                    if state is not None:
                        return exact_thread, state

            conversation_threads = list(
                (
                    await session.scalars(
                        select(ChannelConversation.thread_id)
                        .where(
                            ChannelConversation.business_id == incoming.business_id,
                            ChannelConversation.participant_identifier == participant,
                            ChannelConversation.status == "active",
                        )
                        .order_by(ChannelConversation.updated_at.desc())
                    )
                ).all()
            )
            if conversation_threads:
                selected = await self._select_unique_resumable_thread(
                    conversation_threads,
                    sender=participant,
                )
                if selected[0]:
                    return selected

            # Compatibility fallback for records created before
            # channel_conversations existed. Recipient matching is what
            # connects a website inquiry to its later outbound email.
            interaction_threads = list(
                (
                    await session.scalars(
                        select(Interaction.thread_id)
                        .where(
                            Interaction.business_id == incoming.business_id,
                            or_(
                                Interaction.sender == participant,
                                Interaction.recipient == participant,
                            ),
                            Interaction.thread_id.is_not(None),
                        )
                        .order_by(Interaction.occurred_at.desc())
                        .limit(20)
                    )
                ).all()
            )
        return await self._select_unique_resumable_thread(
            interaction_threads,
            sender=participant,
        )

    async def _upsert_conversation(
        self,
        session,
        *,
        incoming: IncomingInquiry,
        thread_id: str,
        customer_id: str | None,
        lead_id: str | None,
    ) -> None:
        conversation = await session.scalar(
            select(ChannelConversation).where(
                ChannelConversation.business_id == incoming.business_id,
                ChannelConversation.thread_id == thread_id,
                ChannelConversation.channel == incoming.channel,
            )
        )
        message_id = (
            incoming.metadata.get("message_id")
            or incoming.external_event_id
        )
        if conversation is None:
            conversation = ChannelConversation(
                business_id=incoming.business_id,
                customer_id=customer_id,
                lead_id=lead_id,
                thread_id=thread_id,
                channel=incoming.channel,
                channel_source_id=incoming.channel_source_id,
                participant_identifier=incoming.sender_identifier.strip().lower(),
                external_conversation_id=message_id,
                root_message_id=message_id,
            )
            session.add(conversation)
        conversation.customer_id = customer_id or conversation.customer_id
        conversation.lead_id = lead_id or conversation.lead_id
        conversation.latest_message_id = message_id
        conversation.status = "active"

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
        try:
            continuation_thread_id, previous_state = (
                await self._continuation_state(incoming)
            )
        except Exception as exc:
            await fail_request(
                claim.event_id,
                exc,
                session_factory=self.session_factory,
            )
            raise
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
                    external_message_id=(
                        incoming.metadata.get("message_id")
                        or incoming.external_event_id
                    ),
                    sender=incoming.sender_identifier,
                    subject=incoming.subject,
                    content=incoming.text,
                    status="received",
                    metadata={
                        "provider": incoming.provider,
                        "channel_source_id": incoming.channel_source_id,
                        "sender_name": incoming.sender_name,
                        "in_reply_to": incoming.metadata.get("in_reply_to"),
                        "references": incoming.metadata.get("references", []),
                        "attachments": [
                            attachment.model_dump(mode="json")
                            for attachment in incoming.attachments
                        ],
                    },
                )
                ingestion.interaction_id = interaction.id
                ingestion.thread_id = thread_id
                await self._upsert_conversation(
                    session,
                    incoming=incoming,
                    thread_id=thread_id,
                    customer_id=previous_state.get("customer_id"),
                    lead_id=previous_state.get("lead_id"),
                )
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
                    update(ChannelConversation)
                    .where(
                        ChannelConversation.business_id == incoming.business_id,
                        ChannelConversation.thread_id == thread_id,
                    )
                    .values(
                        customer_id=result.get("customer_id"),
                        lead_id=result.get("lead_id"),
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
