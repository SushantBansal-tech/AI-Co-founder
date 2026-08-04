import hashlib
import hmac
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.channels.jobs import ChannelJobService
from app.channels.whatsapp import router
from app.database import ChannelInboundJob, ChannelSource


class NoopIngestion:
    async def ingest(self, incoming):
        return {"ingestion_id": None, "thread_id": "unused"}


def signed(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()


async def make_source(session_factory, monkeypatch):
    suffix = str(uuid4())
    secret_env = f"WA_SECRET_{suffix.replace('-', '_')}"
    verify_env = f"WA_VERIFY_{suffix.replace('-', '_')}"
    monkeypatch.setenv(secret_env, "app-secret")
    monkeypatch.setenv(verify_env, "verify-token")
    source = ChannelSource(
        business_id=f"wa-tenant-{suffix}",
        channel="whatsapp",
        provider="meta_cloud",
        provider_account_id=f"phone-{suffix}",
        public_key=f"whatsapp-{suffix}",
        name="WhatsApp",
        configuration={
            "app_secret_env": secret_env,
            "verify_token_env": verify_env,
        },
    )
    async with session_factory() as session:
        session.add(source)
        await session.commit()
    return source


def payload(source, message_id="wamid.001", phone_id=None):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "metadata": {
                                "phone_number_id": (
                                    phone_id
                                    or source.provider_account_id
                                )
                            },
                            "contacts": [
                                {
                                    "wa_id": "919999999999",
                                    "profile": {"name": "Steel Buyer"},
                                }
                            ],
                            "messages": [
                                {
                                    "from": "919999999999",
                                    "id": message_id,
                                    "timestamp": "1770000000",
                                    "type": "text",
                                    "text": {
                                        "body": "Need 100 MT steel billets"
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ],
    }


@pytest.mark.asyncio
async def test_whatsapp_verification_signature_and_idempotency(
    test_session_factory,
    monkeypatch,
):
    source = await make_source(test_session_factory, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = test_session_factory
    app.state.channel_job_service = ChannelJobService(
        session_factory=test_session_factory,
        ingestion_service=NoopIngestion(),
    )
    transport = httpx.ASGITransport(app=app)
    url = f"/channels/whatsapp/{source.public_key}/webhook"
    raw = json.dumps(payload(source), separators=(",", ":")).encode()

    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        verification = await client.get(
            url,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-token",
                "hub.challenge": "12345",
            },
        )
        rejected = await client.post(
            url,
            content=raw,
            headers={
                "content-type": "application/json",
                "X-Hub-Signature-256": "sha256=wrong",
            },
        )
        headers = {
            "content-type": "application/json",
            "X-Hub-Signature-256": signed(raw, "app-secret"),
        }
        first = await client.post(url, content=raw, headers=headers)
        duplicate = await client.post(url, content=raw, headers=headers)

    assert verification.status_code == 200
    assert verification.text == "12345"
    assert rejected.status_code == 401
    assert first.status_code == 202
    assert first.json()[0]["duplicate"] is False
    assert duplicate.json()[0]["duplicate"] is True
    async with test_session_factory() as session:
        assert await session.scalar(
            select(func.count(ChannelInboundJob.id))
        ) == 1


@pytest.mark.asyncio
async def test_whatsapp_phone_number_prevents_cross_tenant_routing(
    test_session_factory,
    monkeypatch,
):
    source = await make_source(test_session_factory, monkeypatch)
    app = FastAPI()
    app.include_router(router)
    app.state.session_factory = test_session_factory
    app.state.channel_job_service = ChannelJobService(
        session_factory=test_session_factory,
        ingestion_service=NoopIngestion(),
    )
    body = json.dumps(
        payload(source, phone_id="different-tenant-phone"),
        separators=(",", ":"),
    ).encode()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.post(
            f"/channels/whatsapp/{source.public_key}/webhook",
            content=body,
            headers={
                "content-type": "application/json",
                "X-Hub-Signature-256": signed(body, "app-secret"),
            },
        )
    assert response.status_code == 202
    assert response.json() == []
