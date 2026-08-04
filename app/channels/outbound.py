import asyncio
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid

import httpx
from sqlalchemy import select

from app.database import ChannelSource


@dataclass(frozen=True)
class MessageSendResult:
    provider_message_id: str
    status: str

    @property
    def confirmed(self) -> bool:
        return self.status in {"sent", "accepted"}


class SmtpEmailSender:
    async def send(
        self,
        *,
        recipient: str,
        subject: str,
        text: str,
        configuration: dict,
        html: str | None = None,
    ) -> MessageSendResult:
        def _send() -> str:
            username = os.getenv(configuration["username_env"])
            password = os.getenv(configuration["password_env"])
            if not username or not password:
                raise RuntimeError("SMTP credentials are missing.")
            message = EmailMessage()
            message["From"] = configuration.get("from_address", username)
            message["To"] = recipient
            message["Subject"] = subject
            message["Message-ID"] = make_msgid()
            message.set_content(text)
            if html:
                message.add_alternative(html, subtype="html")
            with smtplib.SMTP_SSL(
                configuration["host"],
                int(configuration.get("port", 465)),
            ) as client:
                client.login(username, password)
                client.send_message(message)
            return str(message["Message-ID"] or "")

        provider_id = await asyncio.to_thread(_send)
        return MessageSendResult(provider_id, "sent")


class WhatsAppCloudSender:
    async def send_text(
        self,
        *,
        phone_number_id: str,
        recipient: str,
        text: str,
        access_token_env: str,
        api_version: str = "v23.0",
    ) -> MessageSendResult:
        token = os.getenv(access_token_env)
        if not token:
            raise RuntimeError("WhatsApp access token is missing.")
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                (
                    f"https://graph.facebook.com/{api_version}/"
                    f"{phone_number_id}/messages"
                ),
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": recipient,
                    "type": "text",
                    "text": {"body": text},
                },
            )
            response.raise_for_status()
            payload = response.json()
        message_id = (payload.get("messages") or [{}])[0].get("id", "")
        if not message_id:
            raise RuntimeError(
                "WhatsApp accepted the request without a message ID."
            )
        return MessageSendResult(message_id, "accepted")


class ChannelOutboundDispatcher:
    def __init__(
        self,
        *,
        session_factory,
        email_sender: SmtpEmailSender | None = None,
        whatsapp_sender: WhatsAppCloudSender | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.email_sender = email_sender or SmtpEmailSender()
        self.whatsapp_sender = (
            whatsapp_sender or WhatsAppCloudSender()
        )

    async def _source(
        self,
        *,
        business_id: str,
        channel: str,
    ) -> ChannelSource:
        async with self.session_factory() as session:
            source = await session.scalar(
                select(ChannelSource)
                .where(
                    ChannelSource.business_id == business_id,
                    ChannelSource.channel == channel,
                    ChannelSource.active.is_(True),
                )
                .order_by(ChannelSource.created_at)
                .limit(1)
            )
        if source is None:
            raise RuntimeError(
                f"No active {channel} source is configured for "
                f"business_id={business_id}."
            )
        return source

    async def send(
        self,
        *,
        business_id: str,
        channel: str,
        recipient: str,
        subject: str,
        text: str,
        html: str | None = None,
    ) -> MessageSendResult:
        if not recipient:
            raise RuntimeError("Outbound recipient is required.")
        source = await self._source(
            business_id=business_id,
            channel=channel,
        )
        if channel == "email":
            configuration = (
                source.configuration.get("smtp")
                or source.configuration
            )
            result = await self.email_sender.send(
                recipient=recipient,
                subject=subject,
                text=text,
                html=html,
                configuration=configuration,
            )
        elif channel == "whatsapp":
            if not source.provider_account_id:
                raise RuntimeError(
                    "WhatsApp source has no provider phone number ID."
                )
            result = await self.whatsapp_sender.send_text(
                phone_number_id=source.provider_account_id,
                recipient=recipient,
                text=text,
                access_token_env=source.configuration.get(
                    "access_token_env",
                    "WHATSAPP_ACCESS_TOKEN",
                ),
                api_version=source.configuration.get(
                    "api_version",
                    "v23.0",
                ),
            )
        else:
            raise RuntimeError(
                f"Unsupported outbound channel: {channel}"
            )
        if not result.confirmed:
            raise RuntimeError(
                f"{channel} provider did not confirm the message."
            )
        return result
