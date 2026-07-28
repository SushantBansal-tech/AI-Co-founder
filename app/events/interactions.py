from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.activity import Interaction


async def record_interaction(
    session: AsyncSession,
    *,
    business_id: str,
    direction: str,
    channel: str,
    message_type: str,
    content: str,
    status: str,
    customer_id: Optional[str] = None,
    lead_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    external_message_id: Optional[str] = None,
    sender: Optional[str] = None,
    recipient: Optional[str] = None,
    subject: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> Interaction:
    interaction = Interaction(
        business_id=business_id,
        customer_id=customer_id,
        lead_id=lead_id,
        thread_id=thread_id,
        direction=direction,
        channel=channel,
        message_type=message_type,
        external_message_id=external_message_id,
        sender=sender,
        recipient=recipient,
        subject=subject,
        content=content,
        status=status,
        metadata_json=metadata or {},
    )
    session.add(interaction)
    await session.flush()
    return interaction
