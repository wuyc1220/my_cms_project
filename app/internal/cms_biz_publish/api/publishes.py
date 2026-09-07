"""
发布管理（Publish）API 路由层。

路由前缀：/publishes

接口列表：
    GET    /                         查询发布任务列表（分页 + 多维过滤）
    POST   /batch-publish            批量发布
    POST   /batch-unpublish          批量下架
    POST   /{entity_type}/{entity_id}/publish        立即发布
    POST   /{entity_type}/{entity_id}/unpublish      立即下架
    POST   /{entity_type}/{entity_id}/plan           设置发布/下架计划
    PUT    /{task_id}/plan           修改发布/下架计划
    DELETE /{task_id}/plan           取消发布/下架计划
    GET    /{entity_type}/{entity_id}/history        查询注入历史
"""
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip, get_db
from app.common.schemas import PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_publish.schemas.publish import (
    BatchPublishRequest,
    BatchPublishResultItem,
    IngestHistoryItem,
    PublishListItem,
    PublishPlanCreate,
    PublishPlanResponse,
    PublishPlanUpdate,
)
from app.internal.cms_biz_publish.services import publish_service
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)

router = APIRouter(prefix="/publishes")


@router.get("/", response_model=PaginatedResponse[PublishListItem])
async def get_publish_list(
    page: int = 1,
    page_size: int = 10,
    content_name: Optional[str] = None,
    content_types: Optional[list[str]] = Query(default=None),
    ingest_statuses: Optional[list[str]] = Query(default=None),
    publish_statuses: Optional[list[str]] = Query(default=None),
    publish_time_from: Optional[str] = None,
    publish_time_to: Optional[str] = None,
    unpublish_time_from: Optional[str] = None,
    unpublish_time_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询发布任务列表"""
    return await publish_service.list_publish_tasks(
        db,
        page=page,
        page_size=page_size,
        content_name=content_name,
        content_types=content_types,
        ingest_statuses=ingest_statuses,
        publish_statuses=publish_statuses,
        publish_time_from=publish_time_from,
        publish_time_to=publish_time_to,
        unpublish_time_from=unpublish_time_from,
        unpublish_time_to=unpublish_time_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.post("/batch-publish", response_model=list[BatchPublishResultItem])
async def batch_publish_api(
    body: BatchPublishRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量发布"""
    import json
    raw_val = json.dumps({
        "entity_type": body.entity_type,
        "entity_ids": body.entity_ids,
        "task_type": body.task_type,
        "execution_mode": body.execution_mode,
        "scheduled_time": str(body.scheduled_time) if body.scheduled_time else None
    }, ensure_ascii=False)
    results = await publish_service.batch_publish(
        db, body, current_user.id, processed_by=current_user.username
    )
    success_count = sum(1 for r in results if r.success)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_BATCH,
        operation_object_code="OBJ_PUBLISH_BATCH", operation_object_params={"name": body.entity_type, "count": len(body.entity_ids)},
        operation_content_code="LOG_PUBLISH_BATCH", operation_content_params={"name": body.entity_type},
        entity_type=body.entity_type,
        entity_id=body.entity_ids[0] if body.entity_ids else None,
        previous_value=None,
        updated_value=f"批量发布 {success_count}/{len(body.entity_ids)} 成功",
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success" if success_count == len(body.entity_ids) else "partial",
    )
    await db.commit()
    return results


@router.post("/batch-unpublish", response_model=list[BatchPublishResultItem])
async def batch_unpublish_api(
    body: BatchPublishRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量下架"""
    import json
    raw_val = json.dumps({
        "entity_type": body.entity_type,
        "entity_ids": body.entity_ids
    }, ensure_ascii=False)
    body.task_type = "unpublish"
    results = await publish_service.batch_publish(
        db, body, current_user.id, processed_by=current_user.username
    )
    success_count = sum(1 for r in results if r.success)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.UNPUBLISH_BATCH,
        operation_object_code="OBJ_UNPUBLISH_BATCH", operation_object_params={"name": body.entity_type, "count": len(body.entity_ids)},
        operation_content_code="LOG_UNPUBLISH_BATCH", operation_content_params={"name": body.entity_type},
        entity_type=body.entity_type,
        entity_id=body.entity_ids[0] if body.entity_ids else None,
        previous_value=None,
        updated_value=f"批量下架 {success_count}/{len(body.entity_ids)} 成功",
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success" if success_count == len(body.entity_ids) else "partial",
    )
    await db.commit()
    return results


@router.post("/{entity_type}/{entity_id}/publish", response_model=PublishPlanResponse)
async def publish_now_api(
    entity_type: str,
    entity_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """立即发布"""
    result = await publish_service.publish_now(
        db, entity_type, entity_id, current_user.id, processed_by=current_user.username
    )
    pt = (
        await db.execute(
            select(PublishTask).where(
                PublishTask.entity_type == entity_type,
                PublishTask.entity_id == entity_id,
                PublishTask.is_deleted.is_(False),
            ).order_by(PublishTask.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    new_data = orm_to_dict(pt) if pt else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "publish_task", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_NOW,
        operation_object_code="OBJ_PUBLISH_BATCH", operation_object_params={"name": entity_type},
        operation_content_code="log.publish.now",
        content_id=entity_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type=entity_type,
        entity_id=entity_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.post("/{entity_type}/{entity_id}/unpublish", response_model=PublishPlanResponse)
async def unpublish_now_api(
    entity_type: str,
    entity_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """立即下架"""
    result = await publish_service.unpublish_now(
        db, entity_type, entity_id, current_user.id, processed_by=current_user.username
    )
    pt = (
        await db.execute(
            select(PublishTask).where(
                PublishTask.entity_type == entity_type,
                PublishTask.entity_id == entity_id,
                PublishTask.is_deleted.is_(False),
            ).order_by(PublishTask.id.desc()).limit(1)
        )
    ).scalar_one_or_none()
    new_data = orm_to_dict(pt) if pt else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "publish_task", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.UNPUBLISH_NOW,
        operation_object_code="OBJ_UNPUBLISH_BATCH", operation_object_params={"name": entity_type},
        operation_content_code="log.unpublish.now",
        content_id=entity_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type=entity_type,
        entity_id=entity_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.post("/{entity_type}/{entity_id}/plan", response_model=PublishPlanResponse)
async def create_publish_plan_api(
    entity_type: str,
    entity_id: int,
    body: PublishPlanCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """设置发布/下架计划"""
    body.entity_type = entity_type
    body.entity_id = entity_id

    result = await publish_service.create_publish_plan(
        db, body, current_user.id, processed_by=current_user.username
    )
    pt = (
        await db.execute(
            select(PublishTask).where(PublishTask.id == result.id, PublishTask.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    new_data = orm_to_dict(pt) if pt else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "publish_task", None, new_data)
    # 根据 task_type 区分发布计划/下架计划
    is_unpublish = pt.task_type == "unpublish" if pt else False
    # execution_mode=now 与“立即发布/立即下架”入口功能相同，统一日志类型，
    # 保证不同入口触发同一功能时 Activity Log 显示一致
    if body.execution_mode == "now":
        operation_type = OperationType.UNPUBLISH_NOW if is_unpublish else OperationType.PUBLISH_NOW
        content_code = "log.unpublish.now" if is_unpublish else "log.publish.now"
        object_code = "OBJ_UNPUBLISH_BATCH" if is_unpublish else "OBJ_PUBLISH_BATCH"
        object_params = {"name": entity_type}
    else:
        operation_type = OperationType.PUBLISH_PLAN_CREATE
        content_code = "log.unpublish.plan.create" if is_unpublish else "log.publish.plan.create"
        object_code = "OBJ_PUBLISH_PLAN"
        object_params = {"name": f"{entity_type} #{entity_id}"}
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=operation_type,
        operation_object_code=object_code, operation_object_params=object_params,
        operation_content_code=content_code,
        content_id=entity_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type=entity_type,
        entity_id=entity_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.get("/{entity_type}/{entity_id}/current-plan")
async def get_current_plan_api(
    entity_type: str,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """获取实体当前的发布计划"""
    result = await publish_service.get_current_plan(db, entity_type, entity_id)
    return result


@router.put("/{task_id}/plan", response_model=PublishPlanResponse)
async def update_publish_plan_api(
    task_id: int,
    body: PublishPlanUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """修改发布/下架计划"""
    old_pt = (
        await db.execute(
            select(PublishTask).where(PublishTask.id == task_id, PublishTask.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    old_data = orm_to_dict(old_pt) if old_pt else None
    result = await publish_service.update_publish_plan(db, task_id, body, processed_by=current_user.username)
    new_pt = (
        await db.execute(
            select(PublishTask).where(PublishTask.id == task_id, PublishTask.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    new_data = orm_to_dict(new_pt) if new_pt else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "publish_task", old_data, new_data)
    # 根据 task_type 区分发布计划/下架计划
    is_unpublish = (old_pt or new_pt) and ((new_pt or old_pt).task_type == "unpublish")
    content_code = "log.unpublish.plan.update" if is_unpublish else "log.publish.plan.update"
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_PLAN_UPDATE,
        operation_object_code="OBJ_PUBLISH_PLAN", operation_object_params={"name": task_id},
        operation_content_code=content_code,
        content_id=old_pt.entity_id if old_pt else None,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="publish_plan",
        entity_id=task_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/{task_id}/plan")
async def cancel_publish_plan_api(
    task_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取消发布/下架计划"""
    old_pt = (
        await db.execute(
            select(PublishTask).where(PublishTask.id == task_id, PublishTask.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    old_data = orm_to_dict(old_pt) if old_pt else None
    success = await publish_service.cancel_publish_plan(db, task_id)
    prev_val, new_val, raw_val = await prepare_log_values(db, "publish_task", old_data, None)
    # 根据 task_type 区分发布计划/下架计划
    is_unpublish = old_pt and old_pt.task_type == "unpublish"
    content_code = "log.unpublish.plan.cancel" if is_unpublish else "log.publish.plan.cancel"
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_PLAN_CANCEL,
        operation_object_code="OBJ_PUBLISH_PLAN", operation_object_params={"name": task_id},
        operation_content_code=content_code,
        content_id=old_pt.entity_id if old_pt else None,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="publish_plan",
        entity_id=task_id,
        ip_address=_get_ip(request),
        result="success" if success else "failure",
    )
    await db.commit()
    return {"success": success}


@router.get("/{entity_type}/{entity_id}/history", response_model=PaginatedResponse[IngestHistoryItem])
async def get_ingest_history(
    entity_type: str,
    entity_id: int,
    page: int = 1,
    page_size: int = 10,
    action: Optional[str] = None,
    status: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询注入历史"""
    return await publish_service.list_ingest_histories(
        db,
        page=page,
        page_size=page_size,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        status=status,
    )


@router.get("/{entity_type}/{entity_id}/publish-status")
async def get_object_publish_status(
    entity_type: str,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询对象发布状态"""
    from app.internal.cms_biz_publish.repositories import publish_repository
    
    status = await publish_repository.get_object_publish_status(
        db, entity_type, entity_id
    )
    
    if not status:
        return {"is_published": False, "first_publish_time": None, "last_publish_time": None}
    
    return {
        "is_published": status.is_published,
        "first_publish_time": status.first_publish_time,
        "last_publish_time": status.last_publish_time,
        "last_action": status.last_action,
    }


@router.get("/{entity_type}/{entity_id}/archive-publish-check")
async def check_archive_publish_status(
    entity_type: str,
    entity_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """发布前预检查：节目单归档产物是否已发布（内容详情页点击发布节点时调用）"""
    return await publish_service.check_archive_publish_status(db, entity_type, entity_id)
