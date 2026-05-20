"""
发布管理模块 - 数据访问层
封装 PublishTask / IngestHistory 等表的 SQLAlchemy 查询操作
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import func, select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import app_tz
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_publish.models.object_publish_status import ObjectPublishStatus
from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory
from app.internal.cms_biz_package.models.package import Content


# ═══════════════════════════════════════════════════════════
# PublishTask 发布任务
# ═══════════════════════════════════════════════════════════

async def get_publish_task_by_id(db: AsyncSession, task_id: int) -> Optional[PublishTask]:
    """按ID查询发布任务"""
    return (await db.execute(
        select(PublishTask).where(PublishTask.id == task_id, PublishTask.is_deleted.is_(False))
    )).scalar_one_or_none()


async def get_latest_task_by_entity(
    db: AsyncSession,
    entity_type: str,
    entity_id: int
) -> Optional[PublishTask]:
    """获取实体的最新发布任务"""
    return (await db.execute(
        select(PublishTask)
        .where(
            PublishTask.entity_type == entity_type,
            PublishTask.entity_id == entity_id,
            PublishTask.is_deleted.is_(False)
        )
        .order_by(PublishTask.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def list_publish_tasks(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    content_name: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    ingest_statuses: Optional[list[str]] = None,
    publish_statuses: Optional[list[str]] = None,
    publish_time_from: Optional[str] = None,
    publish_time_to: Optional[str] = None,
    unpublish_time_from: Optional[str] = None,
    unpublish_time_to: Optional[str] = None,
) -> tuple[list[PublishTask], int]:
    """分页查询发布任务列表"""
    query = select(PublishTask).where(PublishTask.is_deleted.is_(False))

    query = query.where(
        PublishTask.entity_id.in_(
            select(Content.id).where(Content.is_discarded.is_(False))
        )
    )

    # 按内容名称搜索
    if content_name:
        query = query.where(PublishTask.entity_name.ilike(f"%{content_name}%"))

    # 按内容类型筛选
    if content_types:
        query = query.where(PublishTask.content_type.in_(content_types))

    # 按发布状态筛选
    if publish_statuses:
        query = query.where(PublishTask.publish_status.in_(publish_statuses))

    # 按发布时间范围筛选
    if publish_time_from:
        dt = datetime.fromisoformat(publish_time_from)
        query = query.where(PublishTask.publish_time >= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
    if publish_time_to:
        dt = datetime.fromisoformat(publish_time_to)
        query = query.where(PublishTask.publish_time <= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)

    if unpublish_time_from:
        dt = datetime.fromisoformat(unpublish_time_from)
        query = query.where(PublishTask.unpublish_time >= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
    if unpublish_time_to:
        dt = datetime.fromisoformat(unpublish_time_to)
        query = query.where(PublishTask.unpublish_time <= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)

    # 统计总数
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    # 分页查询
    result = await db.execute(
        query.order_by(PublishTask.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return result.scalars().all(), total


async def get_entity_current_publish_status(
    db: AsyncSession,
    entity_type: str,
    entity_id: int
) -> Optional[PublishTask]:
    """获取实体当前的发布任务（最新未删除，含已取消，复用同一条避免脏数据）"""
    return (await db.execute(
        select(PublishTask)
        .where(
            PublishTask.entity_type == entity_type,
            PublishTask.entity_id == entity_id,
            PublishTask.is_deleted.is_(False)
        )
        .order_by(PublishTask.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def create_publish_task(db: AsyncSession, task: PublishTask) -> PublishTask:
    """创建发布任务"""
    db.add(task)
    await db.flush()
    await db.refresh(task)
    return task


async def update_publish_task(db: AsyncSession, task: PublishTask) -> PublishTask:
    """更新发布任务"""
    await db.flush()
    await db.refresh(task)
    return task


async def cancel_publish_task(db: AsyncSession, task_id: int) -> bool:
    """取消发布计划：清空定时时间，重置发布状态，任务保持可用"""
    task = await get_publish_task_by_id(db, task_id)
    if task and task.status == "pending":
        task.scheduled_time = None
        task.publish_status = "none"
        await db.flush()
        return True
    return False


# ═══════════════════════════════════════════════════════════
# Content 内容查询（用于关联显示）
# ═══════════════════════════════════════════════════════════

async def get_content_by_id(db: AsyncSession, content_id: int) -> Optional[Content]:
    """按ID查询内容"""
    return (await db.execute(
        select(Content).where(Content.id == content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )).scalar_one_or_none()


async def get_contents_by_ids(db: AsyncSession, ids: list[int]) -> list[Content]:
    """按ID列表查询内容"""
    return (await db.execute(
        select(Content).where(Content.id.in_(ids), Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )).scalars().all()


async def get_child_contents(db: AsyncSession, parent_id: int) -> list[Content]:
    """查询子内容"""
    return (await db.execute(
        select(Content).where(Content.parent_id == parent_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )).scalars().all()


# ═══════════════════════════════════════════════════════════
# IngestHistory 注入历史
# ═══════════════════════════════════════════════════════════

async def list_ingest_histories(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
) -> tuple[list[IngestHistory], int]:
    """分页查询注入历史"""
    query = select(IngestHistory).where(IngestHistory.is_deleted.is_(False))

    if entity_type:
        query = query.where(IngestHistory.entity_type == entity_type)
    if entity_id:
        query = query.where(IngestHistory.entity_id == entity_id)
    if action:
        query = query.where(IngestHistory.action == action)
    if status:
        query = query.where(IngestHistory.status == status)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    result = await db.execute(
        query.order_by(IngestHistory.create_date.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return result.scalars().all(), total


async def get_ingest_history_by_id(db: AsyncSession, history_id: int) -> Optional[IngestHistory]:
    """按ID查询注入历史"""
    return (await db.execute(
        select(IngestHistory).where(IngestHistory.id == history_id, IngestHistory.is_deleted.is_(False))
    )).scalar_one_or_none()


async def get_publish_task_by_correlate_id(db: AsyncSession, correlate_id: str) -> Optional[PublishTask]:
    """根据 SOAP 关联 ID 查询发布任务"""
    return (await db.execute(
        select(PublishTask).where(PublishTask.correlate_id == correlate_id, PublishTask.is_deleted.is_(False))
    )).scalar_one_or_none()


async def get_ingest_history_by_correlate_id(db: AsyncSession, correlate_id: str) -> Optional[IngestHistory]:
    """根据 SOAP 关联 ID 查询注入历史"""
    return (await db.execute(
        select(IngestHistory).where(IngestHistory.correlate_id == correlate_id, IngestHistory.is_deleted.is_(False))
    )).scalar_one_or_none()


async def create_ingest_history(db: AsyncSession, history: IngestHistory) -> IngestHistory:
    """创建注入历史记录"""
    db.add(history)
    await db.flush()
    await db.refresh(history)
    return history


async def update_ingest_history(db: AsyncSession, history: IngestHistory) -> IngestHistory:
    """更新注入历史记录"""
    await db.flush()
    await db.refresh(history)
    return history


# ═══════════════════════════════════════════════════════════
# ObjectPublishStatus 对象发布状态
# ═══════════════════════════════════════════════════════════

async def get_object_publish_status(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
) -> Optional[ObjectPublishStatus]:
    """
    查询对象的发布状态。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型（Content/Cast/Package/Picture/Movie/Category）
        entity_id: 实体ID
    
    Returns:
        ObjectPublishStatus 或 None
    """
    return (await db.execute(
        select(ObjectPublishStatus).where(
            ObjectPublishStatus.entity_type == entity_type,
            ObjectPublishStatus.entity_id == entity_id,
        )
    )).scalar_one_or_none()


async def get_or_create_object_publish_status(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_id: Optional[int] = None,
) -> ObjectPublishStatus:
    """
    获取或创建对象发布状态记录。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型
        entity_id: 实体ID
        content_id: 关联的主内容ID
    
    Returns:
        ObjectPublishStatus 实例
    """
    status = await get_object_publish_status(db, entity_type, entity_id)
    
    if status is None:
        status = ObjectPublishStatus(
            entity_type=entity_type,
            entity_id=entity_id,
            content_id=content_id,
            is_published=False,
        )
        db.add(status)
        await db.flush()
        await db.refresh(status)
    
    return status


async def update_object_publish_status(
    db: AsyncSession,
    status: ObjectPublishStatus,
) -> ObjectPublishStatus:
    """
    更新对象发布状态。
    
    Args:
        db: 数据库会话
        status: ObjectPublishStatus 实例
    
    Returns:
        更新后的 ObjectPublishStatus
    """
    await db.flush()
    await db.refresh(status)
    return status


async def batch_update_object_publish_status(
    db: AsyncSession,
    statuses: list[ObjectPublishStatus],
) -> None:
    """
    批量更新对象发布状态。
    
    Args:
        db: 数据库会话
        statuses: ObjectPublishStatus 实例列表
    """
    for status in statuses:
        await db.flush()
        await db.refresh(status)
