from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.channel import ChannelIngestion, ChannelSource


async def get_active_source_by_public_key(
    session: AsyncSession,
    *,
    public_key: str,
    channel: str,
) -> ChannelSource | None:
    return await session.scalar(
        select(ChannelSource).where(
            ChannelSource.public_key == public_key,
            ChannelSource.channel == channel,
            ChannelSource.active.is_(True),
        )
    )


async def get_source_for_business(
    session: AsyncSession,
    *,
    source_id: str,
    business_id: str,
) -> ChannelSource | None:
    return await session.scalar(
        select(ChannelSource).where(
            ChannelSource.id == source_id,
            ChannelSource.business_id == business_id,
        )
    )


async def count_recent_source_ingestions(
    session: AsyncSession,
    *,
    channel_source_id: str,
    seconds: int = 60,
) -> int:
    cutoff = datetime.utcnow() - timedelta(seconds=seconds)
    return int(
        await session.scalar(
            select(func.count(ChannelIngestion.id)).where(
                ChannelIngestion.channel_source_id == channel_source_id,
                ChannelIngestion.received_at >= cutoff,
            )
        )
        or 0
    )
