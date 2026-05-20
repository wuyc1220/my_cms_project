"""
内容处理流程 Repository 层。

封装 content_process 表的 SQLAlchemy 查询操作。
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.content_process import ContentProcess


async def add_process(db: AsyncSession, process: ContentProcess) -> None:
    """新增流程节点。"""
    db.add(process)
    await db.flush()


async def list_processes_by_content_id(
    db: AsyncSession, content_id: int
) -> list[ContentProcess]:
    """按 content_id 查询流程节点列表，按创建时间倒序（最新的在前面）。"""
    return (
        await db.execute(
            select(ContentProcess)
            .where(ContentProcess.content_id == content_id, ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False))
            .order_by(ContentProcess.created_at.desc().nulls_last(), ContentProcess.id.desc())
        )
    ).scalars().all()


async def get_process_by_content_id_and_node_code(
    db: AsyncSession, content_id: int, node_code: str
) -> list[ContentProcess]:
    """按 content_id 和节点编码查询流程节点列表。"""
    return (
        await db.execute(
            select(ContentProcess).where(
                ContentProcess.content_id == content_id,
                ContentProcess.node_code == node_code,
                ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
            )
        )
    ).scalars().all()


async def update_process_status(
    db: AsyncSession,
    process_id: int,
    status: str,
    end_dt: datetime | None = None,
) -> bool:
    """更新流程节点状态。"""
    process = (
        await db.execute(select(ContentProcess).where(
            ContentProcess.id == process_id,
            ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False)
        ))
    ).scalar_one_or_none()
    if not process:
        return False
    process.status = status
    if end_dt:
        process.end_dt = end_dt
    return True
