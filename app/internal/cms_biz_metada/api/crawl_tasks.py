"""
爬取任务管理 API 路由
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_metada.models.metadata_enhance import MetadataCrawlTask
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_metada.schemas.metadata_enhance import (
    CrawlConfirmRequest,
    CrawlRequest,
    CrawlResponse,
    MetadataCrawlTaskDetail,
    MetadataCrawlTaskListItem,
)
from app.internal.cms_biz_metada.services.crawl_task_service import (
    batch_delete_tasks,
    confirm_selection,
    delete_task,
    get_task,
    get_task_detail,
    list_tasks,
    retry_task,
    trigger_crawl,
)

router = APIRouter(prefix="/crawl-tasks", tags=["元数据增强-爬取任务管理"])


@router.get("/", response_model=PaginatedResponse[MetadataCrawlTaskListItem])
async def get_crawl_tasks(
    page: int = 1,
    page_size: int = 10,
    source_name: str | None = None,
    object_name: str | None = None,
    object_types: list[str] | None = Query(default=None),
    crawl_statuses: list[str] | None = Query(default=None),
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询爬取任务列表"""
    return await list_tasks(
        db, page, page_size, source_name, object_name, object_types, crawl_statuses, sort_by, sort_order
    )


@router.get("/{task_id}", response_model=MetadataCrawlTaskDetail)
async def get_crawl_task_detail(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """获取爬取任务详情"""
    return await get_task_detail(db, task_id)


@router.post("/crawl", response_model=CrawlResponse)
async def crawl_metadata(
    body: CrawlRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """触发元数据爬取"""
    result = await trigger_crawl(db, body)
    import json as _json
    upd_val = _json.dumps({"object_type": body.object_type, "object_name": body.object_name}, ensure_ascii=False)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CRAWL_TASK_CREATE,
        operation_object=f"{body.object_type} {body.object_name}",
        operation_content_code="LOG_CRAWL_TASK_CREATE", operation_content_params={"name": body.object_name},
        ip_address=_get_ip(request),
        result="success",
        updated_value=upd_val,
        entity_type="crawl_task",
        entity_id=result.progress_items[0].task_id if result.progress_items else None,
    )
    await db.commit()
    return result


@router.post("/{task_id}/retry", response_model=MetadataCrawlTaskListItem)
async def retry_crawl_task(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重试爬取任务"""
    old_task = await get_task(db, task_id)
    old_data = orm_to_dict(old_task)
    task = await retry_task(db, task_id)
    new_data = orm_to_dict(task)
    prev_val, new_val, raw_val = await prepare_log_values(db, "crawl_task", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CRAWL_TASK_RETRY,
        operation_object_code="OBJ_CRAWL_TASK", operation_object_params={"name": task.object_name},
        operation_content_code="LOG_CRAWL_TASK_RETRY", operation_content_params={"id": task_id},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="crawl_task",
        entity_id=task_id,
    )
    await db.commit()
    return MetadataCrawlTaskListItem.model_validate(task)


@router.delete("/batch")
async def batch_delete_crawl_tasks(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量删除爬取任务"""
    tasks = (await db.execute(select(MetadataCrawlTask).where(MetadataCrawlTask.id.in_(body.ids)))).scalars().all()
    task_names = ", ".join([t.object_name for t in tasks]) if tasks else str(body.ids)
    prev_data = [orm_to_dict(t) for t in tasks]
    prev_val, _, raw_val = await prepare_log_values(db, "crawl_task", prev_data, None)
    count = await batch_delete_tasks(db, body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CRAWL_TASK_BATCH_DELETE,
        operation_object_code="OBJ_CRAWL_TASK", operation_object_params={"name": task_names},
        operation_content_code="LOG_CRAWL_TASK_BATCH_DELETE", operation_content_params={"names": task_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="crawl_task",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": True, "count": count}


@router.delete("/{task_id}")
async def delete_crawl_task(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除爬取任务"""
    task = await get_task(db, task_id)
    old_data = orm_to_dict(task)
    prev_val, new_val, raw_val = await prepare_log_values(db, "crawl_task", old_data, None)
    await delete_task(db, task_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CRAWL_TASK_DELETE,
        operation_object_code="OBJ_CRAWL_TASK", operation_object_params={"name": task.object_name},
        operation_content_code="LOG_CRAWL_TASK_DELETE", operation_content_params={"name": task.object_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="crawl_task",
        entity_id=task_id,
    )
    await db.commit()
    return {"success": True}


@router.post("/{task_id}/confirm")
async def confirm_crawl_selection(
    task_id: int,
    body: CrawlConfirmRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """确认选择爬取结果"""
    old_task = await get_task(db, task_id)
    old_data = orm_to_dict(old_task)
    results = await confirm_selection(db, task_id, body)
    new_task = await get_task(db, task_id)
    new_data = orm_to_dict(new_task)
    prev_val, new_val, raw_val = await prepare_log_values(db, "crawl_task", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CRAWL_TASK_CONFIRM,
        operation_object_code="OBJ_CRAWL_TASK", operation_object_params={"name": new_task.object_name},
        operation_content_code="LOG_CRAWL_TASK_CONFIRM", operation_content_params={"id": task_id},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="crawl_task",
        entity_id=task_id,
    )
    await db.commit()
    return {"success": True, "selected_fields": [r.model_dump() for r in results]}
