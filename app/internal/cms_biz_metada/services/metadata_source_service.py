"""
数据源管理 Service

对外暴露：
    list_sources()      — 分页查询数据源
    get_source()        — 获取单条数据源
    create_source()     — 新增数据源
    update_source()     — 编辑数据源
    toggle_status()     — 切换启用/禁用状态
    batch_set_status()  — 批量启用/禁用
    delete_source()     — 逻辑删除
    batch_delete()      — 批量逻辑删除
"""


from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.metadata_enhance import MetadataCrawlTask, MetadataSource
from ..schemas.metadata_enhance import (
    MetadataSourceCreate,
    MetadataSourceListItem,
    MetadataSourceUpdate,
)
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


async def list_sources(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    content_types: list[str] | None = None,
    collect_types: list[str] | None = None,
    statuses: list[str] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[MetadataSourceListItem]:
    """分页查询数据源列表"""
    query = select(MetadataSource).where(MetadataSource.is_deleted.is_(False))

    if name:
        query = query.where(MetadataSource.name.ilike(f"%{name}%"))
    if content_types:
        query = query.where(MetadataSource.content_type.in_(content_types))
    if collect_types:
        query = query.where(MetadataSource.collect_type.in_(collect_types))
    if statuses:
        query = query.where(MetadataSource.status.in_(statuses))

    # 排序
    if sort_by and sort_order:
        sort_column = getattr(MetadataSource, sort_by, None)
        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order == "asc" else sort_column.desc(),
                MetadataSource.id.asc(),
            )
        else:
            query = query.order_by(MetadataSource.id.asc())
    else:
        query = query.order_by(MetadataSource.id.asc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    sources = result.scalars().all()

    items = []
    for s in sources:
        item = MetadataSourceListItem.model_validate(s)
        # 掩码 api_key
        if item.api_key:
            item.api_key = item.api_key[:4] + "****" + item.api_key[-4:] if len(item.api_key) > 8 else "***"
        items.append(item)

    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def get_source(db: AsyncSession, source_id: int) -> MetadataSource:
    """获取单条数据源"""
    source = (
        await db.execute(
            select(MetadataSource).where(
                MetadataSource.id == source_id,
                MetadataSource.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not source:
        raise NotFoundException(ErrorCode.METADATA_SOURCE_NOT_FOUND, get_msg("METADATA_SOURCE_NOT_FOUND"))
    return source


async def create_source(db: AsyncSession, data: MetadataSourceCreate) -> MetadataSource:
    """新增数据源"""
    # 校验：API Key 认证时 api_key 必填
    if data.auth_type == "API_Key" and not data.api_key:
        raise BusinessException(ErrorCode.SOURCE_API_KEY_REQUIRED, get_msg("SOURCE_API_KEY_REQUIRED"))
    source = MetadataSource(
        name=data.name,
        content_type=data.content_type,
        collect_type=data.collect_type,
        url=data.url,
        api_endpoint=data.api_endpoint,
        auth_type=data.auth_type,
        api_key=data.api_key,
        rate_limit=data.rate_limit,
        status=data.status,
        page_url_template=data.page_url_template,
        render_type=data.render_type,
        field_extract_rules=data.field_extract_rules,
    )
    db.add(source)
    await db.flush()
    await db.refresh(source)
    return source


async def update_source(
    db: AsyncSession, source_id: int, data: MetadataSourceUpdate
) -> MetadataSource:
    """编辑数据源"""
    source = await get_source(db, source_id)
    update_fields = data.model_dump(exclude_unset=True)
    for field, value in update_fields.items():
        setattr(source, field, value)

    # 校验：API Key 认证时 api_key 必填
    if source.auth_type == "API_Key" and not source.api_key:
        raise BusinessException(ErrorCode.SOURCE_API_KEY_REQUIRED, get_msg("SOURCE_API_KEY_REQUIRED"))

    await db.flush()
    await db.refresh(source)
    return source


async def toggle_status(db: AsyncSession, source_id: int, new_status: str) -> MetadataSource:
    """切换数据源状态"""
    source = await get_source(db, source_id)

    # 禁用时校验：存在 InProgress 的爬取任务则不允许禁用
    if new_status == "NO":
        await _check_no_inprogress_tasks(db, source_id, action="禁用")

    source.status = new_status
    await db.flush()
    await db.refresh(source)
    return source


async def batch_set_status(db: AsyncSession, ids: list[int], new_status: str) -> int:
    """批量启用/禁用"""
    sources = (
        await db.execute(
            select(MetadataSource).where(
                MetadataSource.id.in_(ids),
                MetadataSource.is_deleted.is_(False),
            )
        )
    ).scalars().all()

    count = 0
    for source in sources:
        # 禁用时校验
        if new_status == "NO":
            inprogress = await _has_inprogress_tasks(db, source.id)
            if inprogress:
                logger.warning("数据源 {} 存在进行中的爬取任务，跳过禁用", source.name)
                continue
        source.status = new_status
        count += 1

    await db.flush()
    return count


async def delete_source(db: AsyncSession, source_id: int) -> None:
    """逻辑删除数据源"""
    source = await get_source(db, source_id)
    # 校验：存在 InProgress 的爬取任务则不允许删除
    await _check_no_inprogress_tasks(db, source_id, action="删除")
    source.is_deleted = True
    await db.flush()


async def batch_delete(db: AsyncSession, ids: list[int]) -> int:
    """批量逻辑删除"""
    sources = (
        await db.execute(
            select(MetadataSource).where(
                MetadataSource.id.in_(ids),
                MetadataSource.is_deleted.is_(False),
            )
        )
    ).scalars().all()

    count = 0
    for source in sources:
        inprogress = await _has_inprogress_tasks(db, source.id)
        if inprogress:
            logger.warning("数据源 {} 存在进行中的爬取任务，跳过删除", source.name)
            continue
        source.is_deleted = True
        count += 1

    await db.flush()
    return count


# ── 内部辅助函数 ──────────────────────────────────────────


async def _has_inprogress_tasks(db: AsyncSession, source_id: int) -> bool:
    """检查数据源下是否存在 InProgress 的爬取任务"""
    result = await db.execute(
        select(func.count()).select_from(MetadataCrawlTask).where(
            MetadataCrawlTask.source_id == source_id,
            MetadataCrawlTask.crawl_status == "InProgress",
            MetadataCrawlTask.is_deleted.is_(False),
        )
    )
    return result.scalar_one() > 0


async def _check_no_inprogress_tasks(db: AsyncSession, source_id: int, action: str) -> None:
    """校验数据源下不存在 InProgress 任务，否则抛出异常"""
    if await _has_inprogress_tasks(db, source_id):
        raise BusinessException(ErrorCode.SOURCE_HAS_INPROGRESS_TASK, get_msg("SOURCE_HAS_INPROGRESS_TASK", action=action))


async def get_enabled_sources_by_type(
    db: AsyncSession, content_type: str
) -> list[MetadataSource]:
    """获取指定内容类型的已启用数据源"""
    result = await db.execute(
        select(MetadataSource).where(
            MetadataSource.content_type == content_type,
            MetadataSource.status == "YES",
            MetadataSource.is_deleted.is_(False),
        )
    )
    return result.scalars().all()
