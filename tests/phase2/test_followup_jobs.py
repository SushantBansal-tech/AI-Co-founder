from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from app.database import (
    FollowUpJob,
    FollowUpJobStatus,
    QuotationRecord,
    QuotationStatus,
)
from app.followups.jobs import FollowUpJobService
from app.followups.service import (
    cancel_open_followup_jobs,
    schedule_quotation_followups,
    utc_now,
)


class Snapshot:
    def __init__(self, values):
        self.values = values


class FollowUpGraph:
    def __init__(self, *, result=None):
        self.result = result or {
            "pipeline_status": "followup_sent",
            "followup_provider_message_id": "provider-confirmed",
            "followup_record_id": str(uuid4()),
        }
        self.states = {}
        self.invocations = []

    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return Snapshot(self.states.get(thread_id, {}))

    async def ainvoke(self, state, config):
        self.invocations.append((state, config))
        return {**state, **self.result}


async def create_sent_quotation(
    session_factory,
    *,
    business_id=None,
    thread_id=None,
):
    business_id = business_id or f"followup-{uuid4()}"
    thread_id = thread_id or str(uuid4())
    quotation = QuotationRecord(
        business_id=business_id,
        customer_id=None,
        thread_id=thread_id,
        quotation_number=f"QT-{uuid4().hex[:12]}",
        inquiry_id=str(uuid4()),
        status=QuotationStatus.SENT,
        buyer_company="Follow-up Test Buyer",
        total_inc_gst=100_000,
        requires_approval=False,
        draft_json="{}",
        html_content="<p>Quotation</p>",
        sent_via="email",
        sent_to="buyer@example.com",
        sent_at=utc_now(),
    )
    async with session_factory() as session:
        session.add(quotation)
        await session.commit()
    return quotation


async def schedule_due_jobs(session_factory, quotation):
    async with session_factory() as session:
        await schedule_quotation_followups(
            session,
            business_id=quotation.business_id,
            customer_id=quotation.customer_id,
            lead_id=None,
            thread_id=quotation.thread_id,
            quotation_id=quotation.id,
            quotation_number=quotation.quotation_number,
            sent_at=quotation.sent_at,
            channel=quotation.sent_via,
            recipient=quotation.sent_to,
        )
        await session.commit()
        await session.execute(
            update(FollowUpJob)
            .where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
            .values(
                scheduled_for=utc_now() - timedelta(seconds=1),
                next_attempt_at=utc_now() - timedelta(seconds=1),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_followup_schedule_is_idempotent(
    test_session_factory,
):
    quotation = await create_sent_quotation(test_session_factory)
    async with test_session_factory() as session:
        for _ in range(2):
            await schedule_quotation_followups(
                session,
                business_id=quotation.business_id,
                customer_id=None,
                lead_id=None,
                thread_id=quotation.thread_id,
                quotation_id=quotation.id,
                quotation_number=quotation.quotation_number,
                sent_at=quotation.sent_at,
                channel="email",
                recipient="buyer@example.com",
            )
        await session.commit()
        count = await session.scalar(
            select(func.count(FollowUpJob.id)).where(
                FollowUpJob.quotation_id == quotation.id
            )
        )
    assert count == 4


@pytest.mark.asyncio
async def test_worker_resumes_same_thread_and_completes(
    test_session_factory,
):
    quotation = await create_sent_quotation(test_session_factory)
    await schedule_due_jobs(test_session_factory, quotation)
    graph = FollowUpGraph()
    graph.states[quotation.thread_id] = {
        "business_id": quotation.business_id,
        "thread_id": quotation.thread_id,
        "pipeline_status": "quotation_sent",
        "order_won": False,
    }
    worker = FollowUpJobService(
        session_factory=test_session_factory,
        sales_graph=graph,
    )

    assert await worker.process_one() is True
    invoked_state, config = graph.invocations[0]
    assert invoked_state["business_id"] == quotation.business_id
    assert invoked_state["followup_attempt"] == 1
    assert config["configurable"]["thread_id"] == quotation.thread_id

    async with test_session_factory() as session:
        job = await session.scalar(
            select(FollowUpJob).where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
        )
    assert job.status == FollowUpJobStatus.COMPLETED.value
    assert job.provider_message_id == "provider-confirmed"
    assert job.completed_at is not None


@pytest.mark.asyncio
async def test_worker_retries_graph_failure_and_dead_letters(
    test_session_factory,
):
    quotation = await create_sent_quotation(test_session_factory)
    await schedule_due_jobs(test_session_factory, quotation)
    graph = FollowUpGraph(result={"error": "provider timeout"})
    graph.states[quotation.thread_id] = {
        "business_id": quotation.business_id,
        "pipeline_status": "quotation_sent",
    }
    worker = FollowUpJobService(
        session_factory=test_session_factory,
        sales_graph=graph,
    )
    async with test_session_factory() as session:
        await session.execute(
            update(FollowUpJob)
            .where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
            .values(max_attempts=1)
        )
        await session.commit()

    assert await worker.process_one() is True
    async with test_session_factory() as session:
        job = await session.scalar(
            select(FollowUpJob).where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
        )
    assert job.status == FollowUpJobStatus.DEAD.value
    assert "provider timeout" in job.last_error


@pytest.mark.asyncio
async def test_worker_retries_without_provider_confirmation(
    test_session_factory,
):
    quotation = await create_sent_quotation(test_session_factory)
    await schedule_due_jobs(test_session_factory, quotation)
    graph = FollowUpGraph(
        result={
            "pipeline_status": "followup_sent",
            "followup_record_id": str(uuid4()),
        }
    )
    graph.states[quotation.thread_id] = {
        "business_id": quotation.business_id,
        "pipeline_status": "quotation_sent",
    }
    worker = FollowUpJobService(
        session_factory=test_session_factory,
        sales_graph=graph,
    )

    assert await worker.process_one() is True
    async with test_session_factory() as session:
        job = await session.scalar(
            select(FollowUpJob).where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
        )
    assert job.status == FollowUpJobStatus.RETRY.value
    assert "confirmation is missing" in job.last_error


@pytest.mark.asyncio
async def test_worker_cancels_when_customer_opted_out(
    test_session_factory,
):
    quotation = await create_sent_quotation(test_session_factory)
    await schedule_due_jobs(test_session_factory, quotation)
    graph = FollowUpGraph()
    graph.states[quotation.thread_id] = {
        "business_id": quotation.business_id,
        "pipeline_status": "quotation_sent",
        "customer_opted_out": True,
    }
    worker = FollowUpJobService(
        session_factory=test_session_factory,
        sales_graph=graph,
    )

    assert await worker.process_one() is True
    assert graph.invocations == []
    async with test_session_factory() as session:
        job = await session.scalar(
            select(FollowUpJob).where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
        )
    assert job.status == FollowUpJobStatus.CANCELLED.value
    assert "opted out" in job.cancellation_reason


@pytest.mark.asyncio
async def test_restart_recovers_stale_followup_claim(
    test_session_factory,
):
    quotation = await create_sent_quotation(test_session_factory)
    await schedule_due_jobs(test_session_factory, quotation)
    async with test_session_factory() as session:
        await session.execute(
            update(FollowUpJob)
            .where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
            .values(
                status=FollowUpJobStatus.PROCESSING.value,
                locked_by="dead-worker",
                locked_at=utc_now() - timedelta(minutes=10),
            )
        )
        await session.commit()

    worker = FollowUpJobService(
        session_factory=test_session_factory,
        sales_graph=FollowUpGraph(),
    )
    assert await worker.recover_stale_jobs(
        stale_seconds=60
    ) == 1
    async with test_session_factory() as session:
        status = await session.scalar(
            select(FollowUpJob.status).where(
                FollowUpJob.quotation_id == quotation.id,
                FollowUpJob.attempt_number == 1,
            )
        )
    assert status == FollowUpJobStatus.RETRY.value


@pytest.mark.asyncio
async def test_reply_cancellation_is_tenant_scoped(
    test_session_factory,
):
    quotation_a = await create_sent_quotation(
        test_session_factory,
        business_id=f"tenant-a-{uuid4()}",
    )
    quotation_b = await create_sent_quotation(
        test_session_factory,
        business_id=f"tenant-b-{uuid4()}",
        thread_id=quotation_a.thread_id,
    )
    await schedule_due_jobs(test_session_factory, quotation_a)
    await schedule_due_jobs(test_session_factory, quotation_b)

    async with test_session_factory() as session:
        cancelled = await cancel_open_followup_jobs(
            session,
            business_id=quotation_a.business_id,
            thread_id=quotation_a.thread_id,
            reason="Customer replied.",
        )
        await session.commit()
        a_statuses = (
            await session.execute(
                select(FollowUpJob.status).where(
                    FollowUpJob.business_id
                    == quotation_a.business_id
                )
            )
        ).scalars().all()
        b_statuses = (
            await session.execute(
                select(FollowUpJob.status).where(
                    FollowUpJob.business_id
                    == quotation_b.business_id
                )
            )
        ).scalars().all()

    assert cancelled == 4
    assert set(a_statuses) == {
        FollowUpJobStatus.CANCELLED.value
    }
    assert FollowUpJobStatus.CANCELLED.value not in b_statuses


@pytest.mark.asyncio
async def test_followup_job_api_is_tenant_scoped_and_requires_key(
    test_session_factory,
    monkeypatch,
):
    import app.main as main_module

    quotation = await create_sent_quotation(test_session_factory)
    await schedule_due_jobs(test_session_factory, quotation)
    monkeypatch.setattr(
        main_module,
        "SessionFactory",
        test_session_factory,
    )
    monkeypatch.setattr(
        main_module,
        "followup_job_service",
        FollowUpJobService(
            session_factory=test_session_factory,
            sales_graph=FollowUpGraph(),
        ),
    )
    transport = httpx.ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        own = await client.get(
            "/followups/jobs",
            params={"business_id": quotation.business_id},
        )
        other = await client.get(
            "/followups/jobs",
            params={"business_id": "another-tenant"},
        )
        missing_key = await client.post(
            f"/followups/jobs/{own.json()[0]['id']}/cancel",
            json={
                "business_id": quotation.business_id,
                "reason": "Test",
            },
        )

    assert own.status_code == 200
    assert len(own.json()) == 4
    assert other.status_code == 200
    assert other.json() == []
    assert missing_key.status_code == 422
