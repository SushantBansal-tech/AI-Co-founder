import asyncio
import os
from datetime import timedelta, timezone
from time import monotonic
from uuid import uuid4

from sqlalchemy import select, update

from app.database import (
    FollowUpJob,
    FollowUpJobStatus,
    FollowUpRecord,
    Interaction,
    QuotationRecord,
    QuotationStatus,
)
from app.events.service import record_business_event
from app.followups.service import (
    OPEN_JOB_STATUSES,
    cancel_open_followup_jobs,
    reconcile_followup_jobs,
    utc_now,
)


CANCEL_PIPELINE_STATUSES = {
    "po_received",
    "po_extracted",
    "won",
    "sales_order_created",
    "handoff_packages_built",
    "handoff_dispatched",
    "handed_off",
    "closed_lost",
}


class FollowUpJobService:
    def __init__(
        self,
        *,
        session_factory,
        sales_graph,
        worker_id: str | None = None,
        max_attempts: int | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.sales_graph = sales_graph
        self.worker_id = worker_id or f"followup-{uuid4()}"
        self.max_attempts = max_attempts or int(
            os.getenv("FOLLOWUP_MAX_ATTEMPTS", "5")
        )

    @staticmethod
    def retry_delay(attempt_count: int) -> timedelta:
        seconds = min(
            30 * (2 ** max(attempt_count - 1, 0)),
            3600,
        )
        return timedelta(seconds=seconds)

    async def claim_due_job(self) -> FollowUpJob | None:
        now = utc_now()
        async with self.session_factory() as session:
            job = await session.scalar(
                select(FollowUpJob)
                .where(
                    FollowUpJob.status.in_(OPEN_JOB_STATUSES),
                    FollowUpJob.scheduled_for <= now,
                    FollowUpJob.next_attempt_at <= now,
                )
                .order_by(
                    FollowUpJob.scheduled_for,
                    FollowUpJob.created_at,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if job is None:
                return None
            job.status = FollowUpJobStatus.PROCESSING.value
            job.locked_at = now
            job.locked_by = self.worker_id
            job.attempt_count += 1
            job.updated_at = now
            await session.commit()
            return job

    async def _record_event(
        self,
        job: FollowUpJob,
        event_type: str,
        data: dict,
    ) -> None:
        async with self.session_factory() as session:
            await record_business_event(
                session,
                business_id=job.business_id,
                customer_id=job.customer_id,
                lead_id=job.lead_id,
                thread_id=job.thread_id,
                event_type=event_type,
                source="followup_worker",
                actor_type="system",
                actor_id=self.worker_id,
                entity_type="followup_job",
                entity_id=job.id,
                data=data,
            )
            await session.commit()

    async def cancel_job(
        self,
        job_id: str,
        *,
        business_id: str,
        reason: str,
    ) -> bool:
        now = utc_now()
        async with self.session_factory() as session:
            result = await session.execute(
                update(FollowUpJob)
                .where(
                    FollowUpJob.id == job_id,
                    FollowUpJob.business_id == business_id,
                    FollowUpJob.status.in_(
                        (
                            *OPEN_JOB_STATUSES,
                            FollowUpJobStatus.PROCESSING.value,
                        )
                    ),
                )
                .values(
                    status=FollowUpJobStatus.CANCELLED.value,
                    cancellation_reason=reason,
                    cancelled_at=now,
                    locked_at=None,
                    locked_by=None,
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def retry_job(
        self,
        job_id: str,
        *,
        business_id: str,
    ) -> bool:
        now = utc_now()
        async with self.session_factory() as session:
            result = await session.execute(
                update(FollowUpJob)
                .where(
                    FollowUpJob.id == job_id,
                    FollowUpJob.business_id == business_id,
                    FollowUpJob.status.in_(
                        (
                            FollowUpJobStatus.DEAD.value,
                            FollowUpJobStatus.RETRY.value,
                        )
                    ),
                )
                .values(
                    status=FollowUpJobStatus.RETRY.value,
                    next_attempt_at=now,
                    locked_at=None,
                    locked_by=None,
                    last_error=None,
                    updated_at=now,
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def _eligibility_reason(
        self,
        job: FollowUpJob,
        state: dict,
    ) -> tuple[str, bool] | None:
        if not state:
            return ("LangGraph checkpoint is missing.", True)
        if state.get("business_id") != job.business_id:
            return ("Tenant/thread mismatch.", True)
        if (
            state.get("do_not_contact")
            or state.get("customer_opted_out")
        ):
            return ("Customer opted out of communication.", False)
        status = state.get("pipeline_status", "")
        if (
            state.get("order_won")
            or status in CANCEL_PIPELINE_STATUSES
        ):
            return (
                f"Pipeline no longer needs follow-up: {status}",
                False,
            )

        async with self.session_factory() as session:
            quotation = await session.get(
                QuotationRecord,
                job.quotation_id,
            )
            if (
                quotation is None
                or quotation.business_id != job.business_id
            ):
                return ("Quotation is missing.", True)
            quotation_status = (
                quotation.status.value
                if hasattr(quotation.status, "value")
                else quotation.status
            )
            if quotation_status != QuotationStatus.SENT.value:
                return (
                    f"Quotation status is {quotation_status}.",
                    False,
                )
            sent_record = await session.scalar(
                select(FollowUpRecord.id).where(
                    FollowUpRecord.business_id == job.business_id,
                    FollowUpRecord.quotation_id == job.quotation_id,
                    FollowUpRecord.attempt_number
                    == job.attempt_number,
                )
            )
            if sent_record:
                return ("Follow-up attempt already sent.", False)
            if quotation.sent_at is not None:
                interaction_cutoff = quotation.sent_at
                if interaction_cutoff.tzinfo is not None:
                    interaction_cutoff = (
                        interaction_cutoff
                        .astimezone(timezone.utc)
                        .replace(tzinfo=None)
                    )
                reply = await session.scalar(
                    select(Interaction.id)
                    .where(
                        Interaction.business_id == job.business_id,
                        Interaction.thread_id == job.thread_id,
                        Interaction.direction == "incoming",
                        Interaction.message_type.in_(
                            (
                                "customer_reply",
                                "po_received",
                            )
                        ),
                        Interaction.occurred_at
                        > interaction_cutoff,
                    )
                    .limit(1)
                )
                if reply:
                    return (
                        "Customer replied after quotation.",
                        False,
                    )
        return None

    async def _mark_cancelled(
        self,
        job: FollowUpJob,
        reason: str,
    ) -> None:
        await self.cancel_job(
            job.id,
            business_id=job.business_id,
            reason=reason,
        )
        await self._record_event(
            job,
            "followup.cancelled",
            {
                "attempt_number": job.attempt_number,
                "reason": reason,
            },
        )

    async def _mark_dead(
        self,
        job: FollowUpJob,
        reason: str,
    ) -> None:
        now = utc_now()
        async with self.session_factory() as session:
            await session.execute(
                update(FollowUpJob)
                .where(
                    FollowUpJob.id == job.id,
                    FollowUpJob.business_id == job.business_id,
                )
                .values(
                    status=FollowUpJobStatus.DEAD.value,
                    last_error=reason,
                    locked_at=None,
                    locked_by=None,
                    updated_at=now,
                )
            )
            await session.commit()
        await self._record_event(
            job,
            "followup.delivery_failed",
            {
                "attempt_number": job.attempt_number,
                "error": reason,
            },
        )

    async def _mark_retry(
        self,
        job: FollowUpJob,
        exc: Exception,
    ) -> None:
        if job.attempt_count >= job.max_attempts:
            await self._mark_dead(job, str(exc))
            return
        now = utc_now()
        async with self.session_factory() as session:
            await session.execute(
                update(FollowUpJob)
                .where(
                    FollowUpJob.id == job.id,
                    FollowUpJob.business_id == job.business_id,
                )
                .values(
                    status=FollowUpJobStatus.RETRY.value,
                    next_attempt_at=(
                        now + self.retry_delay(job.attempt_count)
                    ),
                    last_error=str(exc),
                    locked_at=None,
                    locked_by=None,
                    updated_at=now,
                )
            )
            await session.commit()

    async def _mark_completed(
        self,
        job: FollowUpJob,
        *,
        provider_message_id: str,
        followup_record_id: str,
    ) -> None:
        now = utc_now()
        async with self.session_factory() as session:
            await session.execute(
                update(FollowUpJob)
                .where(
                    FollowUpJob.id == job.id,
                    FollowUpJob.business_id == job.business_id,
                    FollowUpJob.status
                    == FollowUpJobStatus.PROCESSING.value,
                )
                .values(
                    status=FollowUpJobStatus.COMPLETED.value,
                    provider_message_id=provider_message_id,
                    followup_record_id=followup_record_id,
                    completed_at=now,
                    locked_at=None,
                    locked_by=None,
                    last_error=None,
                    updated_at=now,
                )
            )
            await session.commit()
        await self._record_event(
            job,
            "followup.sent",
            {
                "attempt_number": job.attempt_number,
                "provider_message_id": provider_message_id,
                "followup_record_id": followup_record_id,
            },
        )

    async def process_one(self) -> bool:
        job = await self.claim_due_job()
        if job is None:
            return False
        try:
            config = {
                "configurable": {
                    "thread_id": job.thread_id,
                }
            }
            snapshot = await self.sales_graph.aget_state(config)
            state = dict(snapshot.values or {})
            ineligible = await self._eligibility_reason(job, state)
            if ineligible:
                reason, terminal = ineligible
                if terminal:
                    await self._mark_dead(job, reason)
                else:
                    await self._mark_cancelled(job, reason)
                return True

            result = await self.sales_graph.ainvoke(
                {
                    "business_id": job.business_id,
                    "thread_id": job.thread_id,
                    "trigger": "followup",
                    "followup_attempt": job.attempt_number,
                    "outbound_channel": job.channel,
                    "outbound_recipient": job.recipient,
                    "followup_job_id": job.id,
                    "error": None,
                },
                config=config,
            )
            if result.get("error"):
                raise RuntimeError(result["error"])
            if (
                result.get("pipeline_status")
                not in {"followup_sent", "awaiting_customer_reply"}
                or (
                    result.get("pipeline_status") == "awaiting_customer_reply"
                    and result.get("business_milestone") != "followup_sent"
                )
            ):
                raise RuntimeError(
                    "Graph did not confirm follow-up completion."
                )
            provider_message_id = result.get(
                "followup_provider_message_id"
            )
            followup_record_id = result.get(
                "followup_record_id"
            )
            if not provider_message_id or not followup_record_id:
                raise RuntimeError(
                    "Follow-up provider or audit confirmation "
                    "is missing."
                )
            await self._mark_completed(
                job,
                provider_message_id=provider_message_id,
                followup_record_id=followup_record_id,
            )
        except Exception as exc:
            await self._mark_retry(job, exc)
        return True

    async def recover_stale_jobs(
        self,
        *,
        stale_seconds: int = 300,
    ) -> int:
        now = utc_now()
        cutoff = now - timedelta(seconds=stale_seconds)
        async with self.session_factory() as session:
            result = await session.execute(
                update(FollowUpJob)
                .where(
                    FollowUpJob.status
                    == FollowUpJobStatus.PROCESSING.value,
                    FollowUpJob.locked_at < cutoff,
                )
                .values(
                    status=FollowUpJobStatus.RETRY.value,
                    locked_at=None,
                    locked_by=None,
                    next_attempt_at=now,
                    last_error="Recovered stale worker claim.",
                    updated_at=now,
                )
            )
            await session.commit()
            return result.rowcount or 0

    async def reconcile(self) -> int:
        async with self.session_factory() as session:
            created = await reconcile_followup_jobs(
                session,
                max_attempts=self.max_attempts,
            )
            await session.commit()
            return created

    async def cancel_thread_jobs(
        self,
        *,
        business_id: str,
        thread_id: str,
        reason: str,
    ) -> int:
        async with self.session_factory() as session:
            count = await cancel_open_followup_jobs(
                session,
                business_id=business_id,
                thread_id=thread_id,
                reason=reason,
            )
            await session.commit()
            return count

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        idle_seconds: float = 5.0,
        stale_seconds: int = 300,
        reconcile_seconds: float = 600.0,
    ) -> None:
        await self.recover_stale_jobs(
            stale_seconds=stale_seconds
        )
        await self.reconcile()
        last_reconcile = monotonic()
        while not stop_event.is_set():
            if monotonic() - last_reconcile >= reconcile_seconds:
                await self.reconcile()
                last_reconcile = monotonic()
            processed = await self.process_one()
            if not processed:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=idle_seconds,
                    )
                except TimeoutError:
                    pass
