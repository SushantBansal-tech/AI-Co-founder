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


def email_bytes(message_id="<rfq-101@example.com>"):
    message = EmailMessage()
    message["From"] = "Buyer Name <buyer@example.com>"
    message["To"] = "sales@example.com"
    message["Subject"] = "RFQ for steel billets"
    message["Message-ID"] = message_id
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
        email_bytes(),
        source=source,
        uid="41",
        uid_validity="999",
    )
    assert incoming.business_id == "tenant-a"
    assert incoming.external_event_id == "<rfq-101@example.com>"
    assert incoming.sender_identifier == "buyer@example.com"
    assert "100 MT" in incoming.text
    assert incoming.attachments[0].filename == "requirement.pdf"


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
