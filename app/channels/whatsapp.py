import hashlib
import hmac
import os
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.channels.repository import get_active_source_by_public_key
from app.channels.schemas import ChannelJobResponse, IncomingInquiry
from app.database import SessionFactory


router = APIRouter(
    prefix="/channels/whatsapp",
    tags=["WhatsApp capture"],
)


def verify_meta_signature(
    raw_body: bytes,
    signature_header: str | None,
    app_secret: str,
) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        app_secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def _message_text(message: dict) -> str:
    message_type = message.get("type", "unknown")
    if message_type == "text":
        return (message.get("text") or {}).get("body", "").strip()
    if message_type == "button":
        return (message.get("button") or {}).get("text", "").strip()
    if message_type == "interactive":
        interactive = message.get("interactive") or {}
        choice = (
            interactive.get("button_reply")
            or interactive.get("list_reply")
            or {}
        )
        return str(choice.get("title") or choice.get("id") or "").strip()
    if message_type in {"image", "document", "video"}:
        media = message.get(message_type) or {}
        return str(
            media.get("caption")
            or f"Customer sent a WhatsApp {message_type} attachment."
        )
    if message_type == "location":
        location = message.get("location") or {}
        return (
            f"Location: {location.get('name') or ''} "
            f"{location.get('address') or ''} "
            f"({location.get('latitude')}, {location.get('longitude')})"
        ).strip()
    return f"Customer sent an unsupported WhatsApp message type: {message_type}."


def normalize_whatsapp_messages(
    payload: dict,
    *,
    source,
) -> list[IncomingInquiry]:
    normalized: list[IncomingInquiry] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            metadata = value.get("metadata") or {}
            if (
                source.provider_account_id
                and metadata.get("phone_number_id")
                != source.provider_account_id
            ):
                continue
            contacts = {
                item.get("wa_id"): (item.get("profile") or {}).get("name")
                for item in value.get("contacts", [])
            }
            for message in value.get("messages", []):
                timestamp = datetime.now(timezone.utc)
                try:
                    timestamp = datetime.fromtimestamp(
                        int(message.get("timestamp")),
                        tz=timezone.utc,
                    )
                except (TypeError, ValueError, OSError):
                    pass
                sender = str(message.get("from") or "").strip()
                text = _message_text(message)
                if not sender or not text or not message.get("id"):
                    continue
                media = message.get(message.get("type")) or {}
                attachments = []
                if message.get("type") in {"image", "document", "video"}:
                    attachments = [
                        {
                            "provider_file_id": media.get("id"),
                            "filename": media.get("filename")
                            or f"whatsapp-{message['id']}",
                            "content_type": media.get("mime_type"),
                        }
                    ]
                normalized.append(
                    IncomingInquiry(
                        business_id=source.business_id,
                        channel_source_id=source.id,
                        channel="whatsapp",
                        provider=source.provider,
                        external_event_id=message["id"],
                        sender_identifier=sender,
                        sender_name=contacts.get(sender),
                        text=text,
                        received_at=timestamp,
                        attachments=attachments,
                        metadata={
                            "phone_number_id": metadata.get(
                                "phone_number_id"
                            ),
                            "display_phone_number": metadata.get(
                                "display_phone_number"
                            ),
                            "message_type": message.get("type"),
                        },
                    )
                )
    return normalized


async def _source(request: Request, public_key: str):
    factory = getattr(
        request.app.state, "session_factory", SessionFactory
    )
    async with factory() as session:
        source = await get_active_source_by_public_key(
            session,
            public_key=public_key,
            channel="whatsapp",
        )
    if source is None:
        raise HTTPException(status_code=404, detail="WhatsApp source not found.")
    return source


@router.get("/{public_key}/webhook")
async def verify_whatsapp_webhook(
    public_key: str,
    request: Request,
    hub_mode: str = Query(alias="hub.mode"),
    hub_verify_token: str = Query(alias="hub.verify_token"),
    hub_challenge: str = Query(alias="hub.challenge"),
):
    source = await _source(request, public_key)
    token = os.getenv(
        source.configuration.get("verify_token_env", ""),
        "",
    )
    if hub_mode != "subscribe" or not token or not hmac.compare_digest(
        token, hub_verify_token
    ):
        raise HTTPException(status_code=403, detail="Webhook verification failed.")
    return Response(content=hub_challenge, media_type="text/plain")


@router.post(
    "/{public_key}/webhook",
    response_model=list[ChannelJobResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_whatsapp_webhook(
    public_key: str,
    request: Request,
):
    source = await _source(request, public_key)
    raw_body = await request.body()
    app_secret = os.getenv(
        source.configuration.get("app_secret_env", ""),
        "",
    )
    if not app_secret or not verify_meta_signature(
        raw_body,
        request.headers.get("X-Hub-Signature-256"),
        app_secret,
    ):
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

    job_service = getattr(request.app.state, "channel_job_service", None)
    if job_service is None:
        raise HTTPException(
            status_code=503, detail="Channel job service is not initialized."
        )

    responses = []
    for incoming in normalize_whatsapp_messages(payload, source=source):
        job, duplicate = await job_service.enqueue(
            incoming, raw_payload=payload
        )
        responses.append(
            ChannelJobResponse(
                job_id=job.id,
                status=job.status,
                duplicate=duplicate,
            )
        )
    return responses
