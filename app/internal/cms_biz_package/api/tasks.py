"""
任务管理 API 路由层。

路由前缀：/tasks

接口列表：
    GET    /                      查询任务列表（分页 + 过滤）
    GET    /{task_id}             查询单个任务详情
    PUT    /{task_id}/assign      分配任务负责人
    PUT    /batch-assign          批量分配任务负责人
    GET    /{task_id}/history     查询任务操作历史
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip, get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.internal.cms_biz_package.repositories import task_repo
from app.internal.cms_biz_package.schemas.task import (
    BatchAssignRequest,
    TaskAssignRequest,
    TaskDetail,
    TaskHistoryItem,
    TaskListItem,
)
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_package.services import task_service

router = APIRouter(prefix="/tasks")


@router.get("/", response_model=PaginatedResponse[TaskListItem])
async def get_task_list(
    page: int = 1,
    page_size: int = 10,
    task_types: str | None = Query(default=None),
    task_statuses: str | None = Query(default=None),
    assignee_keyword: str | None = None,
    assignee_id: int | None = None,
    assignee_is_null: bool = False,
    content_name: str | None = None,
    content_id: int | None = None,
    content_types: str | None = Query(default=None),
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    end_time_start: datetime | None = None,
    end_time_end: datetime | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    sort_mode: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    _parse_csv = lambda v: [x.strip() for x in v.split(",")] if v else None
    return await task_service.list_tasks(
        db,
        page=page,
        page_size=page_size,
        task_types=_parse_csv(task_types),
        task_statuses=_parse_csv(task_statuses),
        assignee_keyword=assignee_keyword,
        assignee_id=assignee_id,
        assignee_is_null=assignee_is_null,
        content_name=content_name,
        content_id=content_id,
        content_types=_parse_csv(content_types),
        time_start=time_start,
        time_end=time_end,
        end_time_start=end_time_start,
        end_time_end=end_time_end,
        sort_by=sort_by,
        sort_order=sort_order,
        sort_mode=sort_mode,
    )


@router.get("/{task_id}", response_model=TaskDetail)
async def get_task_detail(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await task_service.get_task_detail(db, task_id)


@router.put("/{task_id}/assign", response_model=TaskDetail)
async def assign_task_api(
    task_id: int,
    body: TaskAssignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_task = await task_repo.get_task_by_id(db, task_id)
    old_data = orm_to_dict(old_task) if old_task else None
    task = await task_service.assign_task(
        db, task_id, body, processed_by=current_user.username
    )
    new_task = await task_repo.get_task_by_id(db, task_id)
    new_data = orm_to_dict(new_task) if new_task else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "task", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.TASK_ASSIGN,
        operation_object=f"任务 ID={task_id}",
        operation_content=f"Assigned task: ID={task_id}, assignee_id={body.assignee_id}, update_childs={body.update_childs}",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="task",
        entity_id=task_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return task


@router.put("/batch-assign")
async def batch_assign_tasks_api(
    body: BatchAssignRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    assigned_count = await task_service.batch_assign_tasks(
        db,
        task_ids=body.task_ids,
        assignee_id=body.assignee_id,
        update_childs=body.update_childs,
        processed_by=current_user.username,
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.TASK_BATCH_ASSIGN,
        operation_object=f"任务 {body.task_ids}",
        operation_content=f"批量分配任务: {body.task_ids}",
        entity_type="task",
        entity_id=body.task_ids[0] if body.task_ids else None,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True, "assigned_count": assigned_count}


@router.get("/{task_id}/history", response_model=list[TaskHistoryItem])
async def get_task_history(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await task_service.list_task_history(db, task_id)
