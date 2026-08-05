from datetime import datetime
from email.message import EmailMessage
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.channels.email_adapter import (
    EmailPollingService,
    FetchedEmail,
    parse_email_message,
)
from app.database import ChannelCursor, ChannelInboundJob, ChannelSource


class RecordingJobService:
    def __init__(self):
        self.items = []

    async def enqueue(self, incoming, *, raw_payload=None):
        self.items.append(incoming)
        return object(), False


class FakeMailbox:
    def __init__(self, messages):
        self.messages = messages
        self.last_uid_arguments = []

    def fetch_after(self, source, last_uid, expected_uid_validity):
        self.last_uid_arguments.append(last_uid)
        return [
            message
            for message in self.messages
            if int(message.uid) > last_uid
        ]


class FailingMailbox:
    def fetch_after(self, source, last_uid, expected_uid_validity):
        raise RuntimeError("IMAP authentication failed")


def email_bytes(
    message_id="<rfq-101@example.com>",
    *,
    in_reply_to=None,
    references=None,
):
    message = EmailMessage()
    message["From"] = "Buyer Name <buyer@example.com>"
    message["To"] = "sales@example.com"
    message["Subject"] = "RFQ for steel billets"
    message["Message-ID"] = message_id
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = " ".join(references)
    message.set_content("Please quote 100 MT MS billets.")
    message.add_attachment(
        b"purchase order",
        maintype="application",
        subtype="pdf",
        filename="requirement.pdf",
    )
    return message.as_bytes()


async def create_source(session_factory):
    source = ChannelSource(
        business_id=f"email-{uuid4()}",
        channel="email",
        provider="imap",
        provider_account_id=f"sales-{uuid4()}@example.com",
        public_key=f"email-{uuid4()}",
        name="Sales inbox",
        configuration={},
    )
    async with session_factory() as session:
        session.add(source)
        await session.commit()
    return source


def test_email_mime_normalization():
    source = ChannelSource(
        id=str(uuid4()),
        business_id="tenant-a",
        channel="email",
        provider="imap",
        public_key="email-test",
        name="Email",
    )
    incoming = parse_email_message(
        email_bytes(
            in_reply_to="<quotation-1@example.com>",
            references=[
                "<inquiry-1@example.com>",
                "<quotation-1@example.com>",
            ],
        ),
        source=source,
        uid="41",
        uid_validity="999",
    )
    assert incoming.business_id == "tenant-a"
    assert incoming.external_event_id == "<rfq-101@example.com>"
    assert incoming.sender_identifier == "buyer@example.com"
    assert "100 MT" in incoming.text
    assert incoming.attachments[0].filename == "requirement.pdf"
    assert incoming.metadata["in_reply_to"] == "<quotation-1@example.com>"
    assert incoming.metadata["references"] == [
        "<quotation-1@example.com>",
        "<inquiry-1@example.com>",
    ]


@pytest.mark.asyncio
async def test_imap_cursor_survives_poller_restart(test_session_factory):
    source = await create_source(test_session_factory)
    mailbox = FakeMailbox(
        [
            FetchedEmail("41", "999", email_bytes()),
            FetchedEmail(
                "42",
                "999",
                email_bytes("<rfq-102@example.com>"),
            ),
        ]
    )
    first_jobs = RecordingJobService()
    first = EmailPollingService(
        session_factory=test_session_factory,
        job_service=first_jobs,
        mailbox=mailbox,
    )
    assert await first.poll_source(source) == 2
    assert len(first_jobs.items) == 2

    second_jobs = RecordingJobService()
    restarted = EmailPollingService(
        session_factory=test_session_factory,
        job_service=second_jobs,
        mailbox=mailbox,
    )
    assert await restarted.poll_source(source) == 0
    assert mailbox.last_uid_arguments == [0, 42]
    async with test_session_factory() as session:
        cursor = await session.scalar(select(ChannelCursor))
        assert cursor.cursor_value == "42"
        persisted_source = await session.get(ChannelSource, source.id)
        assert persisted_source.last_successful_poll_at is not None
        assert persisted_source.last_seen_uid == "42"
        assert persisted_source.last_poll_messages_enqueued == 0


@pytest.mark.asyncio
async def test_imap_failure_is_persisted_for_health_status(
    test_session_factory,
):
    source = await create_source(test_session_factory)
    poller = EmailPollingService(
        session_factory=test_session_factory,
        job_service=RecordingJobService(),
        mailbox=FailingMailbox(),
    )
    assert await poller.poll_once() == 0
    async with test_session_factory() as session:
        persisted_source = await session.get(ChannelSource, source.id)
        assert persisted_source.last_poll_completed_at is not None
        assert persisted_source.last_successful_poll_at is None
        assert "IMAP authentication failed" in persisted_source.last_poll_error
