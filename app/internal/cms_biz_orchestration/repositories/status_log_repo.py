"""
内容状态变更日志 Repository 层。

封装 content_status_log 表的 SQLAlchemy 查询操作。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.content_status_log import ContentStatusLog


async def add_status_log(db: AsyncSession, log: ContentStatusLog) -> None:
    """新增状态变更日志记录。"""
    db.add(log)
    await db.flush()


async def list_status_logs_by_content_id(
    db: AsyncSession, content_id: int
) -> list[ContentStatusLog]:
    """按 content_id 查询状态变更日志列表，按处理时间倒序。"""
    return (
        await db.execute(
            select(ContentStatusLog)
            .where(
                ContentStatusLog.content_id == content_id,
                ContentStatusLog.is_deleted.is_(False),
            )
            .order_by(ContentStatusLog.processed_at.desc())
        )
    ).scalars().all()
