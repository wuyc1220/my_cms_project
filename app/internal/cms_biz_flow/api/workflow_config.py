"""
工作流配置 API 路由层。

路由前缀：/workflow

接口列表：
    GET    /configs                      查询流程配置列表
    POST   /configs                      创建流程配置
    GET    /configs/{config_id}          获取流程配置详情
    PUT    /configs/{config_id}          更新流程配置
    DELETE /configs/{config_id}          删除流程配置
    POST   /configs/{config_id}/publish  发布流程配置
    POST   /configs/{config_id}/new-version  基于当前版本创建新草稿
    POST   /configs/batch-publish        批量发布
    GET    /configs/{config_id}/versions 获取版本历史
    GET    /available-nodes              获取可用流程环节
    GET    /published/{belonging}        获取指定模块的已发布流程
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_flow.schemas.workflow_config import (
    WorkflowConfigCreate,
    WorkflowConfigDetail,
    WorkflowConfigListItem,
    WorkflowConfigUpdate,
    WorkflowConfigPublishRequest,
)
from app.internal.cms_biz_flow.services import workflow_config_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict

router = APIRouter(prefix="/workflow", tags=["工作流配置"])


@router.get("/configs", response_model=PaginatedResponse[WorkflowConfigListItem])
async def list_workflow_configs(
    page: int = 1,
    page_size: int = 10,
    process_code: str | None = None,
    process_name: str | None = None,
    belonging: list[str] | None = Query(default=None),
    status: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查询流程配置列表。"""
    return await workflow_config_service.list_workflow_configs(
        db, page, page_size, process_code, process_name, belonging, status, sort_by, sort_order
    )


@router.post("/configs", response_model=WorkflowConfigDetail)
async def create_workflow_config(
    data: WorkflowConfigCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建流程配置。"""
    result = await workflow_config_service.create_workflow_config(db, data, current_user.id)
    new_data = orm_to_dict(result) if hasattr(result, '__tablename__') else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "workflow_config", None, new_data) if new_data else (None, None, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.WORKFLOW_CREATE,
        operation_object_code="OBJ_WORKFLOW_CONFIG", operation_object_params={"name": data.process_code},
            operation_content_code="LOG_WORKFLOW_CREATE", operation_content_params={"code": data.process_code},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="workflow_config",
    )
    await db.commit()
    return result


@router.get("/configs/{config_id}", response_model=WorkflowConfigDetail)
async def get_workflow_config(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取流程配置详情（含节点）。"""
    return await workflow_config_service.get_workflow_config(db, config_id)


@router.put("/configs/{config_id}", response_model=WorkflowConfigDetail)
async def update_workflow_config(
    config_id: int,
    data: WorkflowConfigUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新流程配置。"""
    old = await workflow_config_service.get_workflow_config(db, config_id)
    old_data = orm_to_dict(old) if old else None
    result = await workflow_config_service.update_workflow_config(db, config_id, data, current_user.id)
    new_data = orm_to_dict(result) if result else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "workflow_config", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.WORKFLOW_EDIT,
        operation_object_code="OBJ_WORKFLOW_CONFIG", operation_object_params={"name": config_id},
            operation_content_code="LOG_WORKFLOW_EDIT", operation_content_params={"id": config_id},
        ip_address=_get_ip(request),
        result="success",
        entity_type="workflow_config",
        entity_id=config_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
    )
    await db.commit()
    return result


@router.delete("/configs/{config_id}")
async def delete_workflow_config(
    config_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除流程配置。"""
    old = await workflow_config_service.get_workflow_config(db, config_id)
    old_data = orm_to_dict(old) if old else None
    prev_val, _, raw_val = await prepare_log_values(db, "workflow_config", old_data, None)
    success = await workflow_config_service.delete_workflow_config(db, config_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.WORKFLOW_DELETE,
        operation_object_code="OBJ_WORKFLOW_CONFIG", operation_object_params={"name": config_id},
            operation_content_code="LOG_WORKFLOW_DELETE", operation_content_params={"id": config_id},
        ip_address=_get_ip(request),
        result="success" if success else "failure",
        entity_type="workflow_config",
        entity_id=config_id,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": success}


@router.post("/configs/{config_id}/publish", response_model=WorkflowConfigDetail)
async def publish_workflow_config(
    config_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """发布流程配置。"""
    old = await workflow_config_service.get_workflow_config(db, config_id)
    old_data = orm_to_dict(old) if old else None
    result = await workflow_config_service.publish_workflow_config(db, config_id, current_user.id)
    new_data = orm_to_dict(result) if result else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "workflow_config", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.WORKFLOW_PUBLISH,
        operation_object_code="OBJ_PUBLISH_BATCH", operation_object_params={"name": config_id},
            operation_content_code="LOG_WORKFLOW_PUBLISH", operation_content_params={"id": config_id},
        ip_address=_get_ip(request),
        result="success",
        entity_type="workflow_config",
        entity_id=config_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
    )
    await db.commit()
    return result


@router.post("/configs/{config_id}/unpublish", response_model=WorkflowConfigDetail)
async def unpublish_workflow_config(
    config_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """取消发布流程配置。"""
    old = await workflow_config_service.get_workflow_config(db, config_id)
    old_data = orm_to_dict(old) if old else None
    result = await workflow_config_service.unpublish_workflow_config(db, config_id, current_user.id)
    new_data = orm_to_dict(result) if result else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "workflow_config", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.WORKFLOW_UNPUBLISH,
        operation_object_code="OBJ_WORKFLOW_CONFIG", operation_object_params={"name": config_id},
            operation_content_code="LOG_WORKFLOW_UNPUBLISH", operation_content_params={"id": config_id},
        ip_address=_get_ip(request),
        result="success",
        entity_type="workflow_config",
        entity_id=config_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
    )
    await db.commit()
    return result


@router.post("/configs/{config_id}/new-version", response_model=WorkflowConfigDetail)
async def create_new_version(
    config_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """基于当前配置创建新草稿版本。"""
    old = await workflow_config_service.get_workflow_config(db, config_id)
    old_data = orm_to_dict(old) if old else None
    result = await workflow_config_service.create_new_version(db, config_id, current_user.id)
    new_data = orm_to_dict(result) if result else None
    prev_val, new_val, raw_val = await prepare_log_values(db, "workflow_config", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.WORKFLOW_NEW_VERSION,
        operation_object_code="OBJ_WORKFLOW_VERSION", operation_object_params={"name": config_id},
            operation_content_code="LOG_WORKFLOW_NEW_VERSION", operation_content_params={"id": config_id},
        ip_address=_get_ip(request),
        result="success",
        entity_type="workflow_config",
        entity_id=config_id,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
    )
    await db.commit()
    return result


@router.get("/configs/{config_id}/versions")
async def get_version_history(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取流程配置的版本历史。"""
    return await workflow_config_service.get_version_history(db, config_id)


@router.post("/configs/batch-publish", response_model=list[WorkflowConfigDetail])
async def batch_publish_workflow_configs(
    config_ids: list[int],
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量发布流程配置。"""
    import json
    raw_val = json.dumps({"config_ids": config_ids}, ensure_ascii=False)
    results = await workflow_config_service.batch_publish(db, config_ids, current_user.id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.WORKFLOW_BATCH_PUBLISH,
        operation_object_code="OBJ_WORKFLOW", operation_object_params={"name": config_ids},
            operation_content_code="LOG_WORKFLOW_BATCH_PUBLISH", operation_content_params={"ids": config_ids},
        ip_address=_get_ip(request),
        result="success",
        entity_type="workflow_config",
        entity_id=config_ids[0] if config_ids else None,
        previous_value=None,
        updated_value=f"批量发布 {len(config_ids)} 个工作流配置",
        updated_value_json=raw_val,
    )
    await db.commit()
    return results


@router.get("/available-nodes")
async def get_available_nodes(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取可用的流程环节列表。"""
    return await workflow_config_service.get_available_nodes()


@router.get("/published/{belonging}", response_model=WorkflowConfigDetail | None)
async def get_published_workflow(
    belonging: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取指定模块的已发布流程配置。"""
    return await workflow_config_service.get_published_workflow_for_belonging(db, belonging)
