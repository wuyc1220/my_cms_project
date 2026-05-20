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
    )


@router.post("/batch-publish", response_model=list[PublishPlanResponse])
async def batch_publish_api(
    body: BatchPublishRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量发布"""
    results = await publish_service.batch_publish(
        db, body, current_user.id
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_BATCH,
        operation_object=f"批量发布 {len(body.entity_ids)} 个内容",
        operation_content=f"Batch publish: entity_type={body.entity_type}, count={len(body.entity_ids)}",
        entity_type=body.entity_type,
        entity_id=body.entity_ids[0] if body.entity_ids else None,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return results


@router.post("/batch-unpublish", response_model=list[PublishPlanResponse])
async def batch_unpublish_api(
    body: BatchPublishRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量下架"""
    body.task_type = "unpublish"
    results = await publish_service.batch_publish(
        db, body, current_user.id
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.UNPUBLISH_BATCH,
        operation_object=f"批量下架 {len(body.entity_ids)} 个内容",
        operation_content=f"Batch unpublish: entity_type={body.entity_type}, count={len(body.entity_ids)}",
        entity_type=body.entity_type,
        entity_id=body.entity_ids[0] if body.entity_ids else None,
        ip_address=_get_ip(request),
        result="success",
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
        db, entity_type, entity_id, current_user.id
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
        operation_object=f"发布 {entity_type} #{entity_id}",
        operation_content="log.publish.now",
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
        db, entity_type, entity_id, current_user.id
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
        operation_object=f"下架 {entity_type} #{entity_id}",
        operation_content="log.unpublish.now",
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
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_PLAN_CREATE,
        operation_object=f"设置发布计划 {entity_type} #{entity_id}",
        operation_content="log.publish.plan.create",
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
    result = await publish_service.update_publish_plan(db, task_id, body)
    new_pt = (
        await db.execute(
            select(PublishTask).where(PublishTask.id == task_id, PublishTask.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    new_data = orm_to_dict(new_pt) if new_pt else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "publish_task", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_PLAN_UPDATE,
        operation_object=f"修改发布计划 #{task_id}",
        operation_content="log.publish.plan.update",
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
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PUBLISH_PLAN_CANCEL,
        operation_object=f"取消发布计划 #{task_id}",
        operation_content="log.publish.plan.cancel",
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
