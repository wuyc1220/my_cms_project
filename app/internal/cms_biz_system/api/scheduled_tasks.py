"""定时任务（ScheduledTask）API。"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.scheduled_task import (
    ScheduledTaskDetail,
    ScheduledTaskLogOut,
    ScheduledTaskOut,
    TriggerScheduledTasksRequest,
    TriggerScheduledTasksResponse,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_system.services.scheduled_task_service import (
    get_scheduled_task_detail,
    list_scheduled_task_logs,
    list_scheduled_tasks,
    trigger_scheduled_tasks,
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
    triggered = await trigger_scheduled_tasks(db, body.ids, operator=current_user.username)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULED_TASK_BATCH_TRIGGER,
        operation_object=f"定时任务 {body.ids}",
        operation_content=f"批量触发定时任务: {body.ids}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="scheduled_task",
    )
    await db.commit()
    return TriggerScheduledTasksResponse(success=True, triggered=triggered)
