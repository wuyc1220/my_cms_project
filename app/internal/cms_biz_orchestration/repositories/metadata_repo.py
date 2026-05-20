"""
元数据扩展 Repository 层。

封装 ContentMetadata / SeriesMetadata / ChannelMetadata / ScheduleMetadata
的 SQLAlchemy 查询操作。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.content_metadata import (
    ContentMetadata,
    SeriesMetadata,
    ChannelMetadata,
    ScheduleMetadata,
)


# ═══════════════════════════════════════════════════════════
# ContentMetadata — Program (MOVIE / EPISODE)
# ═══════════════════════════════════════════════════════════

async def get_content_metadata_by_content_id(
    db: AsyncSession, content_id: int
) -> ContentMetadata | None:
    """按 content_id 查询 Program 元数据。"""
    return (
        await db.execute(
            select(ContentMetadata).where(
                ContentMetadata.content_id == content_id,
                ContentMetadata.is_deleted.is_(False), ContentMetadata.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()


async def add_content_metadata(db: AsyncSession, metadata: ContentMetadata) -> None:
    """新增 Program 元数据。"""
    db.add(metadata)


async def delete_content_metadata_by_content_id(db: AsyncSession, content_id: int) -> None:
    """软删除 Program 元数据。"""
    metadata = await get_content_metadata_by_content_id(db, content_id)
    if metadata:
        metadata.is_deleted = True


# ═══════════════════════════════════════════════════════════
# SeriesMetadata — Series (SERIES / SEASON)
# ═══════════════════════════════════════════════════════════

async def get_series_metadata_by_content_id(
    db: AsyncSession, content_id: int
) -> SeriesMetadata | None:
    """按 content_id 查询 Series 元数据。"""
    return (
        await db.execute(
            select(SeriesMetadata).where(
                SeriesMetadata.content_id == content_id,
                SeriesMetadata.is_deleted.is_(False), SeriesMetadata.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()


async def add_series_metadata(db: AsyncSession, metadata: SeriesMetadata) -> None:
    """新增 Series 元数据。"""
    db.add(metadata)


async def delete_series_metadata_by_content_id(db: AsyncSession, content_id: int) -> None:
    """软删除 Series 元数据。"""
    metadata = await get_series_metadata_by_content_id(db, content_id)
    if metadata:
        metadata.is_deleted = True


# ═══════════════════════════════════════════════════════════
# ChannelMetadata — Channel (CHANNEL)
# ═══════════════════════════════════════════════════════════

async def get_channel_metadata_by_content_id(
    db: AsyncSession, content_id: int
) -> ChannelMetadata | None:
    """按 content_id 查询 Channel 元数据。"""
    return (
        await db.execute(
            select(ChannelMetadata).where(
                ChannelMetadata.content_id == content_id,
                ChannelMetadata.is_deleted.is_(False), ChannelMetadata.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()


async def add_channel_metadata(db: AsyncSession, metadata: ChannelMetadata) -> None:
    """新增 Channel 元数据。"""
    db.add(metadata)


async def delete_channel_metadata_by_content_id(db: AsyncSession, content_id: int) -> None:
    """软删除 Channel 元数据。"""
    metadata = await get_channel_metadata_by_content_id(db, content_id)
    if metadata:
        metadata.is_deleted = True


# ═══════════════════════════════════════════════════════════
# ScheduleMetadata — Schedule (SCHEDULE)
# ═══════════════════════════════════════════════════════════

async def get_schedule_metadata_by_content_id(
    db: AsyncSession, content_id: int
) -> ScheduleMetadata | None:
    """按 content_id 查询 Schedule 元数据。"""
    return (
        await db.execute(
            select(ScheduleMetadata).where(
                ScheduleMetadata.content_id == content_id,
                ScheduleMetadata.is_deleted.is_(False), ScheduleMetadata.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()


async def add_schedule_metadata(db: AsyncSession, metadata: ScheduleMetadata) -> None:
    """新增 Schedule 元数据。"""
    db.add(metadata)


async def delete_schedule_metadata_by_content_id(db: AsyncSession, content_id: int) -> None:
    """软删除 Schedule 元数据。"""
    metadata = await get_schedule_metadata_by_content_id(db, content_id)
    if metadata:
        metadata.is_deleted = True
