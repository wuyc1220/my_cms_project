"""
数据源管理 API 路由
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_metada.models.metadata_enhance import MetadataSource
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_metada.schemas.metadata_enhance import (
    BatchStatusRequest,
    MetadataSourceCreate,
    MetadataSourceListItem,
    MetadataSourceStatusToggle,
    MetadataSourceUpdate,
)
from app.internal.cms_biz_metada.services.metadata_source_service import (
    batch_delete,
    batch_set_status,
    create_source,
    delete_source,
    get_source,
    list_sources,
    toggle_status,
    update_source,
)

router = APIRouter(prefix="/metadata-sources", tags=["元数据增强-数据源管理"])


@router.get("/", response_model=PaginatedResponse[MetadataSourceListItem])
async def get_metadata_sources(
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    content_types: list[str] | None = Query(default=None),
    collect_types: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询数据源列表"""
    return await list_sources(
        db, page, page_size, name, content_types, collect_types, statuses, sort_by, sort_order
    )


@router.post("/", response_model=MetadataSourceListItem)
async def create_metadata_source(
    body: MetadataSourceCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """新增数据源"""
    source = await create_source(db, body)
    new_data = orm_to_dict(source)
    prev_val, new_val, raw_val = await prepare_log_values(db, "metadata_source", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_SOURCE_CREATE,
        operation_object_code="OBJ_METADATA_SOURCE", operation_object_params={"name": body.name},
        operation_content_code="LOG_METADATA_SOURCE_CREATE", operation_content_params={"name": body.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="metadata_source",
        entity_id=source.id,
    )
    await db.commit()
    return MetadataSourceListItem.model_validate(source)


@router.put("/{source_id}", response_model=MetadataSourceListItem)
async def update_metadata_source(
    source_id: int,
    body: MetadataSourceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """编辑数据源"""
    old = await get_source(db, source_id)
    old_data = orm_to_dict(old)
    source = await update_source(db, source_id, body)
    new_data = orm_to_dict(source)
    prev_val, new_val, raw_val = await prepare_log_values(db, "metadata_source", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_SOURCE_EDIT,
        operation_object_code="OBJ_METADATA_SOURCE", operation_object_params={"name": old.name},
        operation_content_code="LOG_METADATA_SOURCE_EDIT", operation_content_params={"name": old.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="metadata_source",
        entity_id=source_id,
    )
    await db.commit()
    return MetadataSourceListItem.model_validate(source)


@router.patch("/{source_id}/status")
async def toggle_metadata_source_status(
    source_id: int,
    body: MetadataSourceStatusToggle,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """切换数据源状态"""
    old_source = await get_source(db, source_id)
    old_data = orm_to_dict(old_source)
    source = await toggle_status(db, source_id, body.status)
    new_data = orm_to_dict(source)
    prev_val, new_val, raw_val = await prepare_log_values(db, "metadata_source", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_SOURCE_STATUS,
        operation_object_code="OBJ_METADATA_SOURCE", operation_object_params={"name": source.name},
        operation_content_code="LOG_METADATA_SOURCE_STATUS_ENABLED" if body.status == "YES" else "LOG_METADATA_SOURCE_STATUS_DISABLED",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="metadata_source",
        entity_id=source_id,
    )
    await db.commit()
    return {"success": True, "status": source.status}


@router.post("/batch-enable")
async def batch_enable_sources(
    body: BatchStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量启用数据源"""
    sources = (await db.execute(select(MetadataSource).where(MetadataSource.id.in_(body.ids), MetadataSource.is_deleted.is_(False)))).scalars().all()
    src_names = ", ".join([s.name for s in sources]) if sources else str(body.ids)
    old_data = [orm_to_dict(s) for s in sources]
    count = await batch_set_status(db, body.ids, "YES")
    new_data = [orm_to_dict(s) for s in sources if s.status == "YES"]
    prev_val, new_val, raw_val = await prepare_log_values(db, "metadata_source", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_SOURCE_BATCH_ENABLE,
        operation_object_code="OBJ_METADATA_SOURCE", operation_object_params={"name": src_names},
        operation_content_code="LOG_METADATA_SOURCE_BATCH_ENABLE", operation_content_params={"names": src_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="metadata_source",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": True, "count": count}


@router.post("/batch-disable")
async def batch_disable_sources(
    body: BatchStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量禁用数据源"""
    sources = (await db.execute(select(MetadataSource).where(MetadataSource.id.in_(body.ids), MetadataSource.is_deleted.is_(False)))).scalars().all()
    src_names = ", ".join([s.name for s in sources]) if sources else str(body.ids)
    old_data = [orm_to_dict(s) for s in sources]
    count = await batch_set_status(db, body.ids, "NO")
    new_data = [orm_to_dict(s) for s in sources if s.status == "NO"]
    prev_val, new_val, raw_val = await prepare_log_values(db, "metadata_source", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_SOURCE_BATCH_DISABLE,
        operation_object_code="OBJ_METADATA_SOURCE", operation_object_params={"name": src_names},
        operation_content_code="LOG_METADATA_SOURCE_BATCH_DISABLE", operation_content_params={"names": src_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="metadata_source",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": True, "count": count}


@router.delete("/batch")
async def batch_delete_sources(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量逻辑删除数据源"""
    sources = (await db.execute(select(MetadataSource).where(MetadataSource.id.in_(body.ids), MetadataSource.is_deleted.is_(False)))).scalars().all()
    src_names = ", ".join([s.name for s in sources]) if sources else str(body.ids)
    prev_data = [orm_to_dict(s) for s in sources]
    prev_val, _, raw_val = await prepare_log_values(db, "metadata_source", prev_data, None)
    count = await batch_delete(db, body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_SOURCE_BATCH_DELETE,
        operation_object_code="OBJ_METADATA_SOURCE", operation_object_params={"name": src_names},
        operation_content_code="LOG_METADATA_SOURCE_BATCH_DELETE", operation_content_params={"names": src_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="metadata_source",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": True, "count": count}


@router.delete("/{source_id}")
async def delete_metadata_source(
    source_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """逻辑删除数据源"""
    source = await get_source(db, source_id)
    source_name = source.name
    old_data = orm_to_dict(source)
    prev_val, new_val, raw_val = await prepare_log_values(db, "metadata_source", old_data, None)
    await delete_source(db, source_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_SOURCE_DELETE,
        operation_object_code="OBJ_METADATA_SOURCE", operation_object_params={"name": source_name},
        operation_content_code="LOG_METADATA_SOURCE_DELETE", operation_content_params={"name": source_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="metadata_source",
        entity_id=source_id,
    )
    await db.commit()
    return {"success": True}
