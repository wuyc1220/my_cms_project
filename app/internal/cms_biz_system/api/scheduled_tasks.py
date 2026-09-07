"""定时任务（ScheduledTask）API。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.schemas import PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_system.models.scheduled_task import ScheduledTask
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.scheduled_task import (
    ScheduledTaskDetail,
    ScheduledTaskLogOut,
    ScheduledTaskOut,
    TriggerScheduledTasksRequest,
    TriggerScheduledTasksResponse,
    UpdateCronRequest,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_system.services.scheduled_task_service import (
    get_scheduled_task_detail,
    list_scheduled_task_logs,
    list_scheduled_tasks,
    trigger_scheduled_tasks,
    update_scheduled_task_cron,
)

router = APIRouter(prefix="/scheduled-tasks")


@router.get("/", response_model=PaginatedResponse[ScheduledTaskOut])
async def api_list_scheduled_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_scheduled_tasks(db, page, page_size, sort_by, sort_order)


@router.get("/{task_id}", response_model=ScheduledTaskDetail)
async def api_get_scheduled_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_scheduled_task_detail(db, task_id)


@router.get("/{task_id}/logs", response_model=PaginatedResponse[ScheduledTaskLogOut])
async def api_list_task_logs(
    task_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_scheduled_task_logs(db, task_id, page, page_size)


@router.post("/trigger", response_model=TriggerScheduledTasksResponse)
async def api_trigger_scheduled_tasks(
    body: TriggerScheduledTasksRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tasks = (await db.execute(select(ScheduledTask).where(ScheduledTask.id.in_(body.ids)))).scalars().all()
    task_types = ", ".join([t.task_type for t in tasks]) if tasks else str(body.ids)
    prev_data = [orm_to_dict(t) for t in tasks]
    prev_val, _, raw_val = await prepare_log_values(db, "scheduled_task", prev_data, None)
    triggered = await trigger_scheduled_tasks(db, body.ids, operator=current_user.username)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULED_TASK_BATCH_TRIGGER,
        operation_object_code="OBJ_SCHEDULED_TASK", operation_object_params={"name": task_types},
        operation_content_code="LOG_SCHEDULED_TASK_BATCH_TRIGGER", operation_content_params={"types": task_types},
        ip_address=_get_ip(request),
        result="success",
        entity_type="scheduled_task",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    return TriggerScheduledTasksResponse(success=True, triggered=triggered)


@router.put("/{task_id}/cron", response_model=ScheduledTaskOut)
async def api_update_scheduled_task_cron(
    task_id: int,
    body: UpdateCronRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新定时任务的 Cron 表达式（触发频率）。"""
    task = await db.get(ScheduledTask, task_id)
    if task is None:
        from app.common.core.exceptions import BusinessException, ErrorCode
        from app.common.core.i18n import get_msg
        raise BusinessException(ErrorCode.NOT_FOUND, get_msg("NOT_FOUND"))
    
    prev_cron = task.cron_expression
    prev_data = orm_to_dict(task)
    
    updated_task = await update_scheduled_task_cron(db, task_id, body.cron_expression)
    
    # previous_value / updated_value / updated_value_json 均为 VARCHAR 列，必须写入字符串。
    # 直接传 orm_to_dict 结果（dict）会触发 asyncpg DataError（expected str, got dict），
    # 进而污染整个事务、导致 db.commit() 抛 PendingRollbackError。
    # 统一走 prepare_log_values：生成富化展示值（prev/updated）与原始 diff 快照（json）。
    prev_val, new_val, raw_val = await prepare_log_values(
        db, "scheduled_task", prev_data, orm_to_dict(updated_task)
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULED_TASK_UPDATE,
        operation_object_code="OBJ_SCHEDULED_TASK", operation_object_params={"name": updated_task.task_type},
        operation_content_code="LOG_SCHEDULED_TASK_UPDATE", operation_content_params={"name": updated_task.task_type, "old": prev_cron, "new": body.cron_expression},
        ip_address=_get_ip(request),
        result="success",
        entity_type="scheduled_task",
        entity_id=task_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
    )
    await db.commit()
    return updated_task
