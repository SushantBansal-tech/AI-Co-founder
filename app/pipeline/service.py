from datetime import datetime, timezone

from sqlalchemy import select

from app.database.models.pipeline import PipelineInstance


async def persist_pipeline_snapshot(session_factory, state: dict, result: dict, node: str) -> None:
    business_id = state.get("business_id")
    thread_id = state.get("thread_id")
    if not business_id or not thread_id:
        return
    merged = {**state, **result}
    failure = merged.get("failure") or {}
    async with session_factory() as session:
        instance = await session.scalar(select(PipelineInstance).where(
            PipelineInstance.business_id == business_id,
            PipelineInstance.thread_id == thread_id,
        ))
        if instance is None:
            instance = PipelineInstance(business_id=business_id, thread_id=thread_id)
            session.add(instance)
        instance.customer_id = merged.get("customer_id")
        instance.lead_id = merged.get("lead_id")
        instance.pipeline_status = merged.get("pipeline_status") or "processing"
        instance.business_milestone = merged.get("business_milestone")
        instance.waiting_for = merged.get("waiting_for") or "none"
        instance.approval_stage = merged.get("human_approval_stage")
        instance.status_reason = merged.get("status_reason")
        instance.current_node = node
        instance.failure_category = failure.get("category")
        instance.failure_code = failure.get("code")
        instance.failure_details = failure
        instance.version = (instance.version or 0) + 1
        instance.updated_at = datetime.now(timezone.utc)
        await session.commit()
