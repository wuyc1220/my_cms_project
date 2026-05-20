"""
媒资操作历史 Repository 层。

封装 MovieHistory 表的 SQLAlchemy 查询操作。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.movie_history import MovieHistory


async def add_movie_history(db: AsyncSession, history: MovieHistory) -> None:
    """新增媒资操作历史记录。"""
    db.add(history)


async def list_movie_history_by_content_id(
    db: AsyncSession, content_id: int
) -> list[MovieHistory]:
    """按 content_id 查询媒资操作历史列表，按时间倒序。"""
    result = await db.execute(
        select(MovieHistory)
        .where(
            MovieHistory.content_id == content_id,
            MovieHistory.is_deleted.is_(False),
        )
        .order_by(MovieHistory.created_at.desc())
    )
    return result.scalars().all()
