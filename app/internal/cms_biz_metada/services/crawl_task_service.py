"""
爬取任务管理 Service

对外暴露：
    list_tasks()         — 分页查询爬取任务
    get_task_detail()    — 获取任务详情（含候选值）
    trigger_crawl()      — 触发爬取流程
    retry_task()         — 重试爬取任务
    delete_task()        — 物理删除任务及其详情
    batch_delete_tasks() — 批量物理删除
    confirm_selection()  — 确认选择结果
"""

from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.metadata_enhance import MetadataCrawlDetail, MetadataCrawlTask, MetadataSource
from ..schemas.metadata_enhance import (
    CrawlConfirmRequest,
    CrawlConfirmResult,
    CrawlFieldCandidate,
    CrawlProgressItem,
    CrawlRequest,
    CrawlResponse,
    MetadataCrawlDetailItem,
    MetadataCrawlTaskDetail,
    MetadataCrawlTaskListItem,
)
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode
from app.plugins.manager import find_crawler_for_source, get_plugin_manager

# 近期缓存天数（可配置）
CACHE_DAYS = 7


async def list_tasks(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    source_name: str | None = None,
    object_name: str | None = None,
    object_types: list[str] | None = None,
    crawl_statuses: list[str] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[MetadataCrawlTaskListItem]:
    """分页查询爬取任务"""
    query = select(MetadataCrawlTask).where(MetadataCrawlTask.is_deleted.is_(False))

    if source_name:
        query = query.where(MetadataCrawlTask.source_name.ilike(f"%{source_name}%"))
    if object_name:
        query = query.where(MetadataCrawlTask.object_name.ilike(f"%{object_name}%"))
    if object_types:
        query = query.where(MetadataCrawlTask.object_type.in_(object_types))
    if crawl_statuses:
        query = query.where(MetadataCrawlTask.crawl_status.in_(crawl_statuses))

    # 排序
    if sort_by and sort_order:
        sort_column = getattr(MetadataCrawlTask, sort_by, None)
        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order == "asc" else sort_column.desc(),
                MetadataCrawlTask.id.desc(),
            )
        else:
            query = query.order_by(MetadataCrawlTask.id.desc())
    else:
        query = query.order_by(MetadataCrawlTask.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    tasks = result.scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[MetadataCrawlTaskListItem.model_validate(t) for t in tasks],
    )


async def get_task(db: AsyncSession, task_id: int) -> MetadataCrawlTask:
    """获取爬取任务（不含详情）"""
    task = (
        await db.execute(
            select(MetadataCrawlTask).where(
                MetadataCrawlTask.id == task_id,
                MetadataCrawlTask.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not task:
        raise NotFoundException(ErrorCode.CRAWL_TASK_NOT_FOUND, get_msg("CRAWL_TASK_NOT_FOUND"))
    return task


async def get_task_detail(db: AsyncSession, task_id: int) -> MetadataCrawlTaskDetail:
    """获取爬取任务详情（含候选值）"""
    task = await get_task(db, task_id)

    # 查询候选值
    details_result = await db.execute(
        select(MetadataCrawlDetail).where(MetadataCrawlDetail.task_id == task_id)
    )
    details = details_result.scalars().all()

    return MetadataCrawlTaskDetail(
        id=task.id,
        object_name=task.object_name,
        object_type=task.object_type,
        source_id=task.source_id,
        source_name=task.source_name,
        crawl_status=task.crawl_status,
        error_message=task.error_message,
        created_at=task.created_at,
        completed_at=task.completed_at,
        details=[MetadataCrawlDetailItem.model_validate(d) for d in details],
    )


async def trigger_crawl(
    db: AsyncSession,
    request: CrawlRequest,
) -> CrawlResponse:
    """
    触发爬取流程

    核心逻辑：
    1. 查询所有已启用且类型匹配的数据源
    2. 对每个数据源，先查近期缓存（7天内 Completed 的任务）
    3. 有缓存则直接用，无缓存则创建新任务并执行爬取
    4. 汇总结果返回
    """
    from .metadata_source_service import get_enabled_sources_by_type

    # 获取已启用数据源
    sources = await get_enabled_sources_by_type(db, request.object_type)

    if not sources:
        raise BusinessException(ErrorCode.CRAWL_NO_SOURCE_AVAILABLE, get_msg("CRAWL_NO_SOURCE_AVAILABLE"))

    progress_items: list[CrawlProgressItem] = []
    field_candidates: dict[str, list[CrawlFieldCandidate]] = {}

    for source in sources:
        # 查近期缓存
        cache_task = await _find_recent_cache(
            db, source.id, request.object_name, request.object_type
        )

        if cache_task:
            # 使用缓存
            progress_items.append(CrawlProgressItem(
                task_id=cache_task.id,
                source_name=source.name,
                crawl_status="Completed",
                progress=100,
            ))
            # 提取缓存候选值
            cached_details = await _get_task_details(db, cache_task.id)
            for detail in cached_details:
                if detail.field_code not in field_candidates:
                    field_candidates[detail.field_code] = []
                field_candidates[detail.field_code].append(CrawlFieldCandidate(
                    detail_id=detail.id,
                    field_code=detail.field_code,
                    field_name=detail.field_name,
                    crawl_data=detail.crawl_data,
                    source_name=source.name,
                ))
        else:
            # 创建新爬取任务
            new_task = MetadataCrawlTask(
                object_name=request.object_name,
                object_type=request.object_type,
                source_id=source.id,
                source_name=source.name,
                crawl_status="InProgress",
            )
            db.add(new_task)
            await db.flush()
            await db.refresh(new_task)

            progress_items.append(CrawlProgressItem(
                task_id=new_task.id,
                source_name=source.name,
                crawl_status="InProgress",
                progress=0,
            ))

            # 执行爬取
            try:
                candidates = await _execute_crawl(source, request)

                # 存储候选值
                for c in candidates:
                    detail = MetadataCrawlDetail(
                        task_id=new_task.id,
                        field_name=c["field_name"],
                        field_code=c["field_code"],
                        crawl_data=c["crawl_data"],
                    )
                    db.add(detail)

                new_task.crawl_status = "Completed"
                new_task.completed_at = datetime.now()

                # 更新进度
                for p in progress_items:
                    if p.task_id == new_task.id:
                        p.crawl_status = "Completed"
                        p.progress = 100

                await db.flush()

                # 重新读取候选值（获得ID）
                saved_details = await _get_task_details(db, new_task.id)
                for detail in saved_details:
                    if detail.field_code not in field_candidates:
                        field_candidates[detail.field_code] = []
                    field_candidates[detail.field_code].append(CrawlFieldCandidate(
                        detail_id=detail.id,
                        field_code=detail.field_code,
                        field_name=detail.field_name,
                        crawl_data=detail.crawl_data,
                        source_name=source.name,
                    ))

            except Exception as e:
                logger.error("爬取执行失败: source={} error={}", source.name, str(e))
                new_task.crawl_status = "Failed"
                new_task.error_message = str(e)[:500]
                new_task.completed_at = datetime.now()
                await db.flush()

                for p in progress_items:
                    if p.task_id == new_task.id:
                        p.crawl_status = "Failed"
                        p.progress = 0

    return CrawlResponse(
        object_name=request.object_name,
        object_type=request.object_type,
        progress_items=progress_items,
        field_candidates=field_candidates,
    )


async def retry_task(db: AsyncSession, task_id: int) -> MetadataCrawlTask:
    """重试爬取任务：删除原候选值，重新执行"""
    task = await get_task(db, task_id)

    if task.crawl_status not in ("Failed", "Completed"):
        raise BusinessException(ErrorCode.CRAWL_TASK_RETRY_INVALID_STATUS, get_msg("CRAWL_TASK_RETRY_INVALID_STATUS"))

    # 删除原候选值
    await db.execute(
        delete(MetadataCrawlDetail).where(MetadataCrawlDetail.task_id == task_id)
    )

    # 重置任务状态
    task.crawl_status = "InProgress"
    task.error_message = None
    task.completed_at = None
    await db.flush()

    # 获取数据源
    source = None
    if task.source_id:
        source = (
            await db.execute(
                select(MetadataSource).where(
                    MetadataSource.id == task.source_id,
                    MetadataSource.is_deleted.is_(False)
                )
            )
        ).scalar_one_or_none()

    if not source:
        task.crawl_status = "Failed"
        task.error_message = get_msg("CRAWL_SOURCE_DELETED")
        task.completed_at = datetime.now()
        await db.flush()
        return task

    # 重新执行爬取
    try:
        field_codes = [{"code": "all", "name": "全部字段"}]
        candidates = await _execute_crawl(source, CrawlRequest(
            object_name=task.object_name,
            object_type=task.object_type,
            field_codes=field_codes,
        ))

        for c in candidates:
            detail = MetadataCrawlDetail(
                task_id=task.id,
                field_name=c["field_name"],
                field_code=c["field_code"],
                crawl_data=c["crawl_data"],
            )
            db.add(detail)

        task.crawl_status = "Completed"
        task.completed_at = datetime.now()
        await db.flush()

    except Exception as e:
        logger.error("重试爬取失败: task_id={} error={}", task_id, str(e))
        task.crawl_status = "Failed"
        task.error_message = str(e)[:500]
        task.completed_at = datetime.now()
        await db.flush()

    return task


async def delete_task(db: AsyncSession, task_id: int) -> None:
    """物理删除任务及其详情"""
    task = await get_task(db, task_id)
    # 先删详情
    await db.execute(
        delete(MetadataCrawlDetail).where(MetadataCrawlDetail.task_id == task_id)
    )
    # 再删任务
    await db.delete(task)
    await db.flush()


async def batch_delete_tasks(db: AsyncSession, ids: list[int]) -> int:
    """批量物理删除"""
    # 先删详情
    await db.execute(
        delete(MetadataCrawlDetail).where(MetadataCrawlDetail.task_id.in_(ids))
    )
    # 再删任务
    result = await db.execute(
        delete(MetadataCrawlTask).where(MetadataCrawlTask.id.in_(ids))
    )
    await db.flush()
    return result.rowcount


async def confirm_selection(
    db: AsyncSession, task_id: int, request: CrawlConfirmRequest
) -> list[CrawlConfirmResult]:
    """确认选择结果：标记 is_used，返回选中的数据"""
    await get_task(db, task_id)

    results = []
    for selection in request.selections:
        detail = (
            await db.execute(
                select(MetadataCrawlDetail).where(MetadataCrawlDetail.id == selection.detail_id)
            )
        ).scalar_one_or_none()

        if detail and detail.task_id == task_id:
            detail.is_used = selection.is_used
            if selection.is_used == "YES":
                results.append(CrawlConfirmResult(
                    field_code=detail.field_code,
                    field_name=detail.field_name,
                    crawl_data=detail.crawl_data,
                ))

    await db.flush()
    return results


# ── 内部辅助函数 ──────────────────────────────────────────


async def _find_recent_cache(
    db: AsyncSession,
    source_id: int,
    object_name: str,
    object_type: str,
) -> MetadataCrawlTask | None:
    """查找近期已完成的缓存任务"""
    cutoff = datetime.now() - timedelta(days=CACHE_DAYS)
    result = await db.execute(
        select(MetadataCrawlTask).where(
            MetadataCrawlTask.source_id == source_id,
            MetadataCrawlTask.object_name == object_name,
            MetadataCrawlTask.object_type == object_type,
            MetadataCrawlTask.crawl_status == "Completed",
            MetadataCrawlTask.is_deleted.is_(False),
            MetadataCrawlTask.completed_at >= cutoff,
        ).order_by(MetadataCrawlTask.completed_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _get_task_details(
    db: AsyncSession, task_id: int
) -> list[MetadataCrawlDetail]:
    """获取任务的所有候选值"""
    result = await db.execute(
        select(MetadataCrawlDetail).where(MetadataCrawlDetail.task_id == task_id)
    )
    return result.scalars().all()


async def _execute_crawl(
    source: MetadataSource,
    request: CrawlRequest,
) -> list[dict]:
    """
    执行爬取：通过插件管理器查找匹配的爬虫插件并调用

    插件系统自动匹配数据源名称，无需硬编码 if/else。
    新增爬虫只需实现 hookimpl 并注册到插件管理器即可。
    """
    pm = get_plugin_manager()
    plugin = find_crawler_for_source(pm, source.name)

    if plugin is None:
        logger.warning("暂不支持的数据源: {}", source.name)
        return []

    try:
        search_results = await plugin.crawler_search(
            source_url=source.url or "",
            query=request.object_name,
            object_type=request.object_type,
        )
        if not search_results:
            return []

        first_result = search_results[0]
        external_id = first_result.get("id")
        if not external_id:
            return []

        detail_data = await plugin.crawler_get_detail(
            source_url=source.url or "",
            external_id=external_id,
            object_type=request.object_type,
        )
        if not detail_data:
            return []

        return plugin.crawler_map_fields(
            raw_data=detail_data,
            object_type=request.object_type,
            requested_field_codes=request.field_codes,
        )
    except Exception as e:
        logger.error("插件爬取执行失败: source={} plugin={} error={}", source.name, pm.get_name(plugin), str(e))
        return []
