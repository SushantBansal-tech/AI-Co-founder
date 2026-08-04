from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from app.channels.website import router
from app.database import ChannelSource


class CapturingService:
    def __init__(self):
        self.incoming = None

    async def ingest(self, incoming):
        self.incoming = incoming
        return {
            "ingestion_id": str(uuid4()),
            "interaction_id": str(uuid4()),
            "thread_id": str(uuid4()),
            "state": {"business_id": incoming.business_id},
        }


@pytest.mark.asyncio
async def test_website_api_resolves_tenant_from_public_key(
    test_session_factory,
):
    business_id = f"website-api-{uuid4()}"
    source = ChannelSource(
        business_id=business_id,
        channel="website",
        provider="native_form",
        public_key=f"public-{uuid4()}",
        name="API test",
        active=True,
        configuration={"max_submissions_per_minute": 100},
    )
    async with test_session_factory() as session:
        session.add(source)
        await session.commit()

    service = CapturingService()
    app = FastAPI()
    app.include_router(router)
    app.state.channel_ingestion_service = service
    app.state.session_factory = test_session_factory
    transport = httpx.ASGITransport(app=app)

    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                f"/channels/website/{source.public_key}/inquiries",
                json={
                    "submission_id": "website-submission-0001",
                    "name": "Buyer",
                    "email": "buyer@example.com",
                    "message": "Need 100 MT steel",
                    "business_id": "malicious-tenant-override",
                },
            )
        assert response.status_code == 202
        assert service.incoming.business_id == business_id
        assert response.json()["state"]["business_id"] == business_id
    finally:
        async with test_session_factory() as session:
            await session.delete(
                await session.get(ChannelSource, source.id)
            )
            await session.commit()


@pytest.mark.asyncio
async def test_website_api_validation_and_unknown_source(
    test_session_factory,
):
    app = FastAPI()
    app.include_router(router)
    app.state.channel_ingestion_service = CapturingService()
    app.state.session_factory = test_session_factory
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        invalid = await client.post(
            "/channels/website/unknown/inquiries",
            json={
                "submission_id": "website-submission-0002",
                "name": "Buyer",
                "message": "No contact details",
            },
        )
        unknown = await client.post(
            "/channels/website/unknown/inquiries",
            json={
                "submission_id": "website-submission-0003",
                "name": "Buyer",
                "email": "buyer@example.com",
                "message": "Valid body but unknown source",
            },
        )
    assert invalid.status_code == 422
    assert unknown.status_code == 404
