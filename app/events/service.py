from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.activity import BusinessEvent


async def record_business_event(
    session: AsyncSession,
    *,
    business_id: str,
    event_type: str,
    customer_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    source: str = "graph",
    actor_type: str = "agent",
    actor_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    data: Optional[dict] = None,
) -> BusinessEvent:
    event = BusinessEvent(
        business_id=business_id,
        customer_id=customer_id,
        lead_id=lead_id,
        thread_id=thread_id,
        event_type=event_type,
        source=source,
        actor_type=actor_type,
        actor_id=actor_id,
        entity_type=entity_type,
        entity_id=entity_id,
        data=data or {},
    )
    session.add(event)
    await session.flush()
    return event
