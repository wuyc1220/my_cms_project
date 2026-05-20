"""
工作流配置 Repository 层。

封装 workflow_config 和 workflow_node_config 表的 SQLAlchemy 查询操作。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_flow.models.workflow_config import WorkflowConfig, WorkflowNodeConfig


async def list_workflow_configs(
    db: AsyncSession,
    *,
    process_code: str | None = None,
    process_name: str | None = None,
    belonging: list[str] | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 10,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[WorkflowConfig], int]:
    """分页查询流程配置列表。"""
    query = select(WorkflowConfig).where(WorkflowConfig.is_deleted.is_(False))

    if process_code:
        query = query.where(WorkflowConfig.process_code.ilike(f"%{process_code}%"))
    if process_name:
        query = query.where(WorkflowConfig.process_name.ilike(f"%{process_name}%"))
    if belonging:
        query = query.where(WorkflowConfig.belonging.in_(belonging))
    if status:
        query = query.where(WorkflowConfig.status == status)

    # 动态排序（默认按更新时间倒序）
    if sort_by and sort_order:
        sort_column = getattr(WorkflowConfig, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func)
        else:
            query = query.order_by(WorkflowConfig.updated_at.desc())
    else:
        query = query.order_by(WorkflowConfig.updated_at.desc())

    # 先查询总数（必须在添加 ORDER BY 之前）
    count_query = select(func.count(WorkflowConfig.id)).where(WorkflowConfig.is_deleted.is_(False))
    if process_code:
        count_query = count_query.where(WorkflowConfig.process_code.ilike(f"%{process_code}%"))
    if process_name:
        count_query = count_query.where(WorkflowConfig.process_name.ilike(f"%{process_name}%"))
    if belonging:
        count_query = count_query.where(WorkflowConfig.belonging.in_(belonging))
    if status:
        count_query = count_query.where(WorkflowConfig.status == status)
    
    total = (await db.execute(count_query)).scalar() or 0

    query = query.offset((page - 1) * page_size).limit(page_size)
    items = (await db.execute(query)).scalars().all()

    return list(items), total


async def get_workflow_config_by_id(db: AsyncSession, config_id: int) -> WorkflowConfig | None:
    """根据ID获取流程配置详情"""
    return (
        await db.execute(
            select(WorkflowConfig).where(
                WorkflowConfig.id == config_id,
                WorkflowConfig.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()


async def get_workflow_config_by_code(db: AsyncSession, process_code: str) -> WorkflowConfig | None:
    """根据流程编码获取流程配置（最新版本）。"""
    return (
        await db.execute(
            select(WorkflowConfig)
            .where(
                WorkflowConfig.process_code == process_code,
                WorkflowConfig.is_deleted.is_(False),
            )
            .order_by(WorkflowConfig.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_max_version_by_code(db: AsyncSession, process_code: str) -> int:
    """获取指定流程编码的最大版本号（排除已删除记录）。"""
    result = (
        await db.execute(
            select(func.max(WorkflowConfig.version)).where(
                WorkflowConfig.process_code == process_code,
                WorkflowConfig.is_deleted.is_(False),
            )
        )
    ).scalar()
    return result or 0


async def get_draft_by_code(db: AsyncSession, process_code: str) -> WorkflowConfig | None:
    """获取指定流程编码的草稿版本。"""
    return (
        await db.execute(
            select(WorkflowConfig).where(
                WorkflowConfig.process_code == process_code,
                WorkflowConfig.status == "draft",
                WorkflowConfig.is_deleted.is_(False),
            )
            .order_by(WorkflowConfig.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def get_published_workflow_by_belonging(
    db: AsyncSession, belonging: str
) -> WorkflowConfig | None:
    """获取指定模块的已发布流程配置。"""
    return (
        await db.execute(
            select(WorkflowConfig)
            .where(
                WorkflowConfig.belonging == belonging,
                WorkflowConfig.status == "published",
                WorkflowConfig.is_deleted.is_(False),
            )
            .order_by(WorkflowConfig.published_version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def create_workflow_config(db: AsyncSession, config: WorkflowConfig) -> WorkflowConfig:
    """创建流程配置。"""
    db.add(config)
    await db.flush()
    await db.refresh(config)
    return config


async def update_workflow_config(db: AsyncSession, config: WorkflowConfig) -> WorkflowConfig:
    """更新流程配置。"""
    await db.flush()
    await db.refresh(config)
    return config


async def delete_workflow_config(db: AsyncSession, config_id: int) -> bool:
    """软删除流程配置。"""
    config = await get_workflow_config_by_id(db, config_id)
    if not config:
        return False
    config.is_deleted = True
    await db.flush()
    return True


async def list_workflow_nodes(
    db: AsyncSession, workflow_config_id: int
) -> list[WorkflowNodeConfig]:
    """获取流程配置的所有节点。"""
    return (
        await db.execute(
            select(WorkflowNodeConfig)
            .where(
                WorkflowNodeConfig.workflow_config_id == workflow_config_id,
                WorkflowNodeConfig.is_deleted.is_(False),
            )
            .order_by(WorkflowNodeConfig.sequence.asc())
        )
    ).scalars().all()


async def create_workflow_node(db: AsyncSession, node: WorkflowNodeConfig) -> WorkflowNodeConfig:
    """创建流程节点配置。"""
    db.add(node)
    await db.flush()
    await db.refresh(node)
    return node


async def delete_workflow_nodes_by_config_id(db: AsyncSession, workflow_config_id: int) -> None:
    """硬删除流程配置下的所有节点（避免自增 ID 浪费和旧数据残留）。"""
    from sqlalchemy import delete
    await db.execute(
        delete(WorkflowNodeConfig).where(
            WorkflowNodeConfig.workflow_config_id == workflow_config_id
        )
    )
    await db.flush()


async def publish_workflow_config(db: AsyncSession, config_id: int) -> WorkflowConfig | None:
    """发布流程配置。"""
    config = await get_workflow_config_by_id(db, config_id)
    if not config:
        return None
    config.status = "published"
    config.published_version = config.version
    await db.flush()
    await db.refresh(config)
    return config


async def unpublish_workflow_config(db: AsyncSession, config_id: int) -> WorkflowConfig | None:
    """取消发布流程配置。"""
    config = await get_workflow_config_by_id(db, config_id)
    if not config:
        return None
    config.status = "unpublished"
    config.published_version = None
    await db.flush()
    await db.refresh(config)
    return config


async def get_version_history(db: AsyncSession, process_code: str) -> list[WorkflowConfig]:
    """获取指定流程编码的所有版本历史。"""
    return (
        await db.execute(
            select(WorkflowConfig)
            .where(
                WorkflowConfig.process_code == process_code,
                WorkflowConfig.is_deleted.is_(False),
            )
            .order_by(WorkflowConfig.version.desc())
        )
    ).scalars().all()
