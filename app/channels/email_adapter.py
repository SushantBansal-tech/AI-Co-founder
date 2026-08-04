import asyncio
import imaplib
import os
import socket
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime

from sqlalchemy import select

from app.channels.schemas import AttachmentReference, IncomingInquiry
from app.database import ChannelCursor, ChannelSource

logger = logging.getLogger(__name__)


def _plain_text(message: Message) -> str:
    if message.is_multipart():
        parts: list[str] = []
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() == "text/plain":
                parts.append(part.get_content())
        if parts:
            return "\n".join(parts).strip()
        for part in message.walk():
            if part.get_content_type() == "text/html":
                return str(part.get_content()).strip()
        return ""
    return str(message.get_content()).strip()


def parse_email_message(
    raw_message: bytes,
    *,
    source: ChannelSource,
    uid: str,
    uid_validity: str,
) -> IncomingInquiry:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    sender_name, sender_address = parseaddr(message.get("From", ""))
    if not sender_address:
        raise ValueError("Email message has no valid From address.")

    received_at = datetime.now(timezone.utc)
    if message.get("Date"):
        try:
            received_at = parsedate_to_datetime(message["Date"])
            if received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, OverflowError):
            pass

    attachments = []
    for part in message.iter_attachments():
        content = part.get_payload(decode=True) or b""
        attachments.append(
            AttachmentReference(
                filename=part.get_filename() or "attachment",
                content_type=part.get_content_type(),
                size_bytes=len(content),
            )
        )

    message_id = str(message.get("Message-ID", "")).strip()
    external_id = message_id or (
        f"imap:{source.id}:{uid_validity}:{uid}"
    )
    text = _plain_text(message)
    if not text:
        text = f"Email received with subject: {message.get('Subject', '')}"

    return IncomingInquiry(
        business_id=source.business_id,
        channel_source_id=source.id,
        channel="email",
        provider=source.provider,
        external_event_id=external_id,
        sender_identifier=sender_address.lower(),
        sender_name=sender_name or None,
        subject=str(message.get("Subject", "")) or None,
        text=text,
        received_at=received_at,
        attachments=attachments,
        metadata={
            "imap_uid": uid,
            "uid_validity": uid_validity,
            "message_id": message_id or None,
        },
    )


@dataclass
class FetchedEmail:
    uid: str
    uid_validity: str
    raw_message: bytes


class ImapMailbox:
    def fetch_after(
        self,
        source: ChannelSource,
        last_uid: int,
        expected_uid_validity: str | None,
    ) -> list[FetchedEmail]:
        config = source.configuration
        username = os.getenv(config.get("username_env", ""))
        password = os.getenv(config.get("password_env", ""))
        if not username or not password:
            raise RuntimeError(
                f"IMAP credentials are missing for channel source {source.id}."
            )

        host = config["host"]
        port = int(config.get("port", 993))
        folder = config.get("folder", "INBOX")

        mailbox = imaplib.IMAP4_SSL(host, port)
        try:
            mailbox.login(username, password)
            status, _ = mailbox.select(folder, readonly=True)
            if status != "OK":
                raise RuntimeError(f"Unable to select IMAP folder {folder}.")
            
            uid_validity = str(mailbox.response("UIDVALIDITY")[1][0], "ascii")
            if expected_uid_validity and expected_uid_validity != uid_validity:
                last_uid = 0

            status, data = mailbox.uid(
                "search", None, f"UID {last_uid + 1}:*"
            )
            if status != "OK":
                raise RuntimeError("IMAP UID search failed.")

            fetched: list[FetchedEmail] = []
            for uid_bytes in (data[0] or b"").split():
                try:
                    status, payload = mailbox.uid(
                        "fetch", uid_bytes, "(BODY.PEEK[])"
                    )
                    if status != "OK":
                        continue
                    body = next(
                        (
                            item[1]
                            for item in payload
                            if isinstance(item, tuple)
                            and isinstance(item[1], bytes)
                        ),
                        None,
                    )
                    if body is not None:
                        fetched.append(
                            FetchedEmail(
                                uid=uid_bytes.decode("ascii"),
                                uid_validity=uid_validity,
                                raw_message=body,
                            )
                        )
                except (imaplib.IMAP4.abort, ConnectionResetError, socket.error) as err:
                    logger.warning("Connection dropped during message fetch (UID %s): %s", uid_bytes, err)
                    break  # Stop processing remaining messages in this pass; process retrieved ones safely.

            return fetched
        finally:
            try:
                mailbox.logout()
            except Exception:
                pass


class EmailPollingService:
    def __init__(
        self,
        *,
        session_factory,
        job_service,
        mailbox: ImapMailbox | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.job_service = job_service
        self.mailbox = mailbox or ImapMailbox()

    async def poll_source(self, source: ChannelSource) -> int:
        async with self.session_factory() as session:
            cursor = await session.scalar(
                select(ChannelCursor).where(
                    ChannelCursor.channel_source_id == source.id,
                    ChannelCursor.cursor_type == "imap_uid",
                )
            )
            last_uid = int(cursor.cursor_value or 0) if cursor else 0
            old_validity = (cursor.metadata_json or {}).get("uid_validity") if cursor else None

        # Add exponential retries for network-level issues (e.g., DNS failures, connection drops)
        messages = []
        max_retries = 3
        for attempt in range(max_retries):
            try:
                messages = await asyncio.to_thread(
                    self.mailbox.fetch_after,
                    source,
                    last_uid,
                    old_validity,
                )
                break
            except (socket.gaierror, ConnectionResetError, imaplib.IMAP4.abort) as err:
                if attempt == max_retries - 1:
                    raise
                logger.warning("Transient network error polling source %s (attempt %d): %s", source.id, attempt + 1, err)
                await asyncio.sleep(2 ** attempt)

        processed = 0
        for fetched in messages:
            effective_last_uid = (
                0
                if old_validity and old_validity != fetched.uid_validity
                else last_uid
            )
            if int(fetched.uid) <= effective_last_uid:
                continue

            incoming = parse_email_message(
                fetched.raw_message,
                source=source,
                uid=fetched.uid,
                uid_validity=fetched.uid_validity,
            )
            await self.job_service.enqueue(
                incoming,
                raw_payload={
                    "imap_uid": fetched.uid,
                    "uid_validity": fetched.uid_validity,
                },
            )

            async with self.session_factory() as session:
                current = await session.scalar(
                    select(ChannelCursor).where(
                        ChannelCursor.channel_source_id == source.id,
                        ChannelCursor.cursor_type == "imap_uid",
                    )
                )
                if current is None:
                    current = ChannelCursor(
                        business_id=source.business_id,
                        channel_source_id=source.id,
                        cursor_type="imap_uid",
                    )
                    session.add(current)
                current.cursor_value = fetched.uid
                current.metadata_json = {
                    "uid_validity": fetched.uid_validity
                }
                await session.commit()

            last_uid = int(fetched.uid)
            old_validity = fetched.uid_validity
            processed += 1

        return processed

    async def poll_once(self) -> int:
        async with self.session_factory() as session:
            sources = (
                await session.execute(
                    select(ChannelSource).where(
                        ChannelSource.channel == "email",
                        ChannelSource.provider == "imap",
                        ChannelSource.active.is_(True),
                    )
                )
            ).scalars().all()
        total = 0
        for source in sources:
            try:
                total += await self.poll_source(source)
            except Exception:
                logger.exception(
                    "Email polling failed for source %s",
                    source.id,
                )
        return total

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        interval_seconds: float = 30.0,
    ) -> None:
        while not stop_event.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=interval_seconds
                )
            except TimeoutError:
                pass