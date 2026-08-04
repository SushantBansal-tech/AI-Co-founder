from datetime import datetime
from typing import Protocol

from fastapi import APIRouter, HTTPException, Request, status

from app.channels.repository import (
    count_recent_source_ingestions,
    get_active_source_by_public_key,
)
from app.channels.schemas import (
    ChannelIngestionResponse,
    IncomingInquiry,
    WebsiteInquiryRequest,
    website_request_to_text,
)
from app.database import SessionFactory
from app.idempotency.service import (
    IdempotencyConflict,
    IdempotencyInProgress,
)


router = APIRouter(prefix="/channels/website", tags=["Website capture"])


class CaptchaVerifier(Protocol):
    async def verify(
        self,
        *,
        token: str | None,
        remote_ip: str | None,
    ) -> bool: ...


class RejectRequiredCaptchaVerifier:
    async def verify(
        self,
        *,
        token: str | None,
        remote_ip: str | None,
    ) -> bool:
        return False


@router.post(
    "/{public_key}/inquiries",
    response_model=ChannelIngestionResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def capture_website_inquiry(
    public_key: str,
    payload: WebsiteInquiryRequest,
    request: Request,
):
    session_factory = getattr(
        request.app.state,
        "session_factory",
        SessionFactory,
    )
    async with session_factory() as session:
        source = await get_active_source_by_public_key(
            session,
            public_key=public_key,
            channel="website",
        )
        if source is None:
            raise HTTPException(
                status_code=404,
                detail="Website inquiry source not found.",
            )

        maximum_per_minute = int(
            source.configuration.get("max_submissions_per_minute", 30)
        )
        recent_count = await count_recent_source_ingestions(
            session,
            channel_source_id=source.id,
        )
        if recent_count >= maximum_per_minute:
            raise HTTPException(
                status_code=429,
                detail="Website inquiry rate limit exceeded.",
            )

    if source.configuration.get("require_consent") and not payload.consent:
        raise HTTPException(
            status_code=422,
            detail="Consent is required for this inquiry form.",
        )

    if source.configuration.get("require_captcha"):
        verifier = getattr(
            request.app.state,
            "captcha_verifier",
            RejectRequiredCaptchaVerifier(),
        )
        remote_ip = request.client.host if request.client else None
        if not await verifier.verify(
            token=payload.captcha_token,
            remote_ip=remote_ip,
        ):
            raise HTTPException(
                status_code=403,
                detail="CAPTCHA verification failed.",
            )

    sender_identifier = payload.email or payload.phone
    incoming = IncomingInquiry(
        business_id=source.business_id,
        channel_source_id=source.id,
        channel="website",
        provider=source.provider,
        external_event_id=payload.submission_id,
        sender_identifier=sender_identifier,
        sender_name=payload.name,
        subject=f"Website inquiry from {payload.name}",
        text=website_request_to_text(payload),
        received_at=datetime.utcnow(),
        metadata={
            "raw_payload": payload.model_dump(mode="json"),
            "public_key": public_key,
            "consent": payload.consent,
        },
    )

    service = getattr(
        request.app.state,
        "channel_ingestion_service",
        None,
    )
    if service is None:
        raise HTTPException(
            status_code=503,
            detail="Channel ingestion service is not initialized.",
        )

    try:
        return await service.ingest(incoming)
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except IdempotencyInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Website inquiry processing failed: {exc}",
        ) from exc
