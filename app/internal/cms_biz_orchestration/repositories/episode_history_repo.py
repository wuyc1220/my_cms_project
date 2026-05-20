"""
单集操作历史 Repository 层。

封装 EpisodeHistory 表的 SQLAlchemy 查询操作。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.episode_history import EpisodeHistory


async def add_episode_history(db: AsyncSession, history: EpisodeHistory) -> None:
    """新增单集操作历史记录。"""
    db.add(history)


async def list_episode_history_by_parent_id(
    db: AsyncSession, parent_id: int
) -> list[EpisodeHistory]:
    """按 parent_id 查询单集操作历史列表，按时间倒序。"""
    result = await db.execute(
        select(EpisodeHistory)
        .where(
            EpisodeHistory.parent_id == parent_id,
            EpisodeHistory.is_deleted.is_(False),
        )
        .order_by(EpisodeHistory.created_at.desc())
    )
    return result.scalars().all()


async def list_episode_history_by_parent_id_with_filters(
    db: AsyncSession,
    parent_id: int,
    content_name: str | None = None,
    processed_type: str | None = None,
    processed_by: str | None = None,
) -> list[EpisodeHistory]:
    """按 parent_id 和筛选条件查询单集操作历史列表，按时间倒序。"""
    query = select(EpisodeHistory).where(
        EpisodeHistory.parent_id == parent_id,
        EpisodeHistory.is_deleted.is_(False),
    )

    if content_name:
        query = query.where(EpisodeHistory.content_name.ilike(f"%{content_name}%"))
    if processed_type:
        query = query.where(EpisodeHistory.processed_type == processed_type)
    if processed_by:
        query = query.where(EpisodeHistory.processed_by.ilike(f"%{processed_by}%"))

    query = query.order_by(EpisodeHistory.created_at.desc())
    result = await db.execute(query)
    return result.scalars().all()
