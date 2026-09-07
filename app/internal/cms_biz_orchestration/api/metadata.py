"""
元数据管理 API 路由层。

路由前缀：/metadata

接口列表：
- 统一查询：
    GET    /metadata/{content_id}              查询内容元数据详情（根据 content_type 自动路由）

- Program 元数据 (MOVIE/EPISODE)：
    GET    /metadata/{content_id}/program      查询 Program 元数据
    POST   /metadata/{content_id}/program      创建 Program 元数据
    PUT    /metadata/{content_id}/program      更新 Program 元数据
    DELETE /metadata/{content_id}/program      删除 Program 元数据

- Series 元数据 (SERIES/SEASON)：
    GET    /metadata/{content_id}/series       查询 Series 元数据
    POST   /metadata/{content_id}/series       创建 Series 元数据
    PUT    /metadata/{content_id}/series       更新 Series 元数据
    DELETE /metadata/{content_id}/series       删除 Series 元数据

- Channel 元数据 (CHANNEL)：
    GET    /metadata/{content_id}/channel      查询 Channel 元数据
    POST   /metadata/{content_id}/channel      创建 Channel 元数据
    PUT    /metadata/{content_id}/channel      更新 Channel 元数据
    DELETE /metadata/{content_id}/channel      删除 Channel 元数据

- Schedule 元数据 (SCHEDULE)：
    GET    /metadata/{content_id}/schedule     查询 Schedule 元数据
    POST   /metadata/{content_id}/schedule     创建 Schedule 元数据
    PUT    /metadata/{content_id}/schedule     更新 Schedule 元数据
    DELETE /metadata/{content_id}/schedule     删除 Schedule 元数据
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_metada.models.basic import CustomTag
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.internal.cms_biz_orchestration.schemas.content_metadata import (
    ContentMetadataCreate,
    ContentMetadataItem,
    ContentMetadataUpdate,
    SeriesMetadataCreate,
    SeriesMetadataItem,
    SeriesMetadataUpdate,
    ChannelMetadataCreate,
    ChannelMetadataItem,
    ChannelMetadataUpdate,
    ScheduleMetadataCreate,
    ScheduleMetadataItem,
    ScheduleMetadataUpdate,
    MetadataDetailItem,
)
from app.internal.cms_biz_orchestration.services import metadata_service
from app.internal.cms_biz_package.models.package import Content, ContentCustomTag


router = APIRouter(prefix="/metadata", tags=["元数据管理"])


async def _load_custom_tag_names(db: AsyncSession, content_id: int) -> list[str]:
    """查询内容关联的自定义标签名称列表。

    Custom Tags 存于 content_custom_tag 中间表（元数据表无此列，弹窗保存时经
    updateContent 单独提交），日志快照补记名称供 Activity Log 展示（bug 32430）。
    """
    rows = (await db.execute(
        select(ContentCustomTag.custom_tag_id, CustomTag.name)
        .join(CustomTag, ContentCustomTag.custom_tag_id == CustomTag.id)
        .where(ContentCustomTag.content_id == content_id, CustomTag.is_deleted.is_(False))
    )).all()
    return [row.name for row in rows]


# ═══════════════════════════════════════════════════════════
# 统一查询接口
# ═══════════════════════════════════════════════════════════

@router.get("/{content_id}", response_model=MetadataDetailItem)
async def get_metadata_detail(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询内容元数据详情。

    根据 content.content_type 自动返回对应类型的元数据。
    """
    return await metadata_service.get_metadata_detail(db, content_id)


# ═══════════════════════════════════════════════════════════
# Program 元数据 (MOVIE / EPISODE)
# ═══════════════════════════════════════════════════════════

@router.get("/{content_id}/program", response_model=ContentMetadataItem | None)
async def get_program_metadata(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询 Program 元数据。"""
    return await metadata_service.get_content_metadata(db, content_id)


@router.post("/{content_id}/program", response_model=ContentMetadataItem)
async def create_program_metadata(
    content_id: int,
    data: ContentMetadataCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建 Program 元数据。"""
    # 强制使用 URL 中的 content_id
    data_dict = data.model_dump()
    data_dict["content_id"] = content_id
    result = await metadata_service.create_content_metadata(
        db, ContentMetadataCreate(**data_dict), processed_by=current_user.username
    )
    new_data = orm_to_dict(result, "program_metadata")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "program_metadata", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PROGRAM_METADATA_CREATE,
        operation_object_code="OBJ_PROGRAM_METADATA", operation_object_params={"name": result.name},
        operation_content_code="log.metadata.create",
        content_id=content_id,
        entity_type="program_metadata",
        entity_id=result.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.put("/{content_id}/program", response_model=ContentMetadataItem)
async def update_program_metadata(
    content_id: int,
    data: ContentMetadataUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新 Program 元数据。"""
    import copy
    old = await metadata_service.get_content_metadata(db, content_id)
    old_data = copy.deepcopy(orm_to_dict(old, "program_metadata")) if old else {}
    result = await metadata_service.update_content_metadata(db, content_id, data, processed_by=current_user.username)
    new_data = orm_to_dict(result, "program_metadata")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "program_metadata", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.PROGRAM_METADATA_UPDATE,
            operation_object_code="OBJ_PROGRAM_METADATA", operation_object_params={"name": result.name},
            operation_content_code="log.metadata.edit",
            content_id=content_id,
            entity_type="program_metadata",
            entity_id=result.id,
            previous_value=prev_val,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{content_id}/program")
async def delete_program_metadata(
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除 Program 元数据。"""
    old = await metadata_service.get_content_metadata(db, content_id)
    old_data = orm_to_dict(old, "program_metadata") if old else {}
    meta_name = old.name if old else f"ID={content_id}"
    await metadata_service.delete_content_metadata(db, content_id)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "program_metadata", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PROGRAM_METADATA_DELETE,
        operation_object_code="OBJ_PROGRAM_METADATA", operation_object_params={"name": meta_name},
        operation_content_code="log.metadata.delete",
        content_id=content_id,
        entity_type="program_metadata",
        entity_id=old.id if old else None,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


# ═══════════════════════════════════════════════════════════
# Series 元数据 (SERIES / SEASON)
# ═══════════════════════════════════════════════════════════

@router.get("/{content_id}/series", response_model=SeriesMetadataItem | None)
async def get_series_metadata(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询 Series 元数据。"""
    return await metadata_service.get_series_metadata(db, content_id)


@router.post("/{content_id}/series", response_model=SeriesMetadataItem)
async def create_series_metadata(
    content_id: int,
    data: SeriesMetadataCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建 Series 元数据。"""
    data_dict = data.model_dump()
    data_dict["content_id"] = content_id
    result = await metadata_service.create_series_metadata(
        db, SeriesMetadataCreate(**data_dict), processed_by=current_user.username
    )
    new_data = orm_to_dict(result, "series_metadata")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "series_metadata", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SERIES_METADATA_CREATE,
        operation_object_code="OBJ_SERIES_METADATA", operation_object_params={"name": result.name},
        operation_content_code="log.metadata.create",
        content_id=content_id,
        entity_type="series_metadata",
        entity_id=result.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.put("/{content_id}/series", response_model=SeriesMetadataItem)
async def update_series_metadata(
    content_id: int,
    data: SeriesMetadataUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新 Series 元数据。"""
    import copy
    old = await metadata_service.get_series_metadata(db, content_id)
    old_data = copy.deepcopy(orm_to_dict(old, "series_metadata")) if old else {}
    result = await metadata_service.update_series_metadata(
        db, content_id, data, processed_by=current_user.username,
        actor_id=current_user.id, ip_address=_get_ip(request),
    )
    new_data = orm_to_dict(result, "series_metadata")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "series_metadata", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.SERIES_METADATA_UPDATE,
            operation_object_code="OBJ_SERIES_METADATA", operation_object_params={"name": result.name},
            operation_content_code="log.metadata.edit",
            content_id=content_id,
            entity_type="series_metadata",
            entity_id=result.id,
            previous_value=prev_val,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{content_id}/series")
async def delete_series_metadata(
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除 Series 元数据。"""
    old = await metadata_service.get_series_metadata(db, content_id)
    old_data = orm_to_dict(old, "series_metadata") if old else {}
    meta_name = old.name if old else f"ID={content_id}"
    await metadata_service.delete_series_metadata(db, content_id)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "series_metadata", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SERIES_METADATA_DELETE,
        operation_object_code="OBJ_SERIES_METADATA", operation_object_params={"name": meta_name},
        operation_content_code="log.metadata.delete",
        content_id=content_id,
        entity_type="series_metadata",
        entity_id=old.id if old else None,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


# ═══════════════════════════════════════════════════════════
# Channel 元数据 (CHANNEL)
# ═══════════════════════════════════════════════════════════

@router.get("/{content_id}/channel", response_model=ChannelMetadataItem | None)
async def get_channel_metadata(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询 Channel 元数据。"""
    return await metadata_service.get_channel_metadata(db, content_id)


@router.post("/{content_id}/channel", response_model=ChannelMetadataItem)
async def create_channel_metadata(
    content_id: int,
    data: ChannelMetadataCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建 Channel 元数据。"""
    data_dict = data.model_dump()
    data_dict["content_id"] = content_id
    result = await metadata_service.create_channel_metadata(
        db, ChannelMetadataCreate(**data_dict), processed_by=current_user.username
    )
    new_data = orm_to_dict(result, "channel_metadata")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "channel_metadata", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CHANNEL_METADATA_CREATE,
        operation_object_code="OBJ_CHANNEL", operation_object_params={"name": result.name},
        operation_content_code="log.metadata.create",
        content_id=content_id,
        entity_type="channel_metadata",
        entity_id=result.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.put("/{content_id}/channel", response_model=ChannelMetadataItem)
async def update_channel_metadata(
    content_id: int,
    data: ChannelMetadataUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新 Channel 元数据。"""
    import copy
    old = await metadata_service.get_channel_metadata(db, content_id)
    old_data = copy.deepcopy(orm_to_dict(old, "channel_metadata")) if old else {}
    result = await metadata_service.update_channel_metadata(db, content_id, data, processed_by=current_user.username)
    new_data = orm_to_dict(result, "channel_metadata")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "channel_metadata", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CHANNEL_METADATA_UPDATE,
            operation_object_code="OBJ_CHANNEL", operation_object_params={"name": result.name},
            operation_content_code="log.metadata.edit",
            content_id=content_id,
            entity_type="channel_metadata",
            entity_id=result.id,
            previous_value=prev_val,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{content_id}/channel")
async def delete_channel_metadata(
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除 Channel 元数据。"""
    old = await metadata_service.get_channel_metadata(db, content_id)
    old_data = orm_to_dict(old, "channel_metadata") if old else {}
    meta_name = old.name if old else f"ID={content_id}"
    await metadata_service.delete_channel_metadata(db, content_id)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "channel_metadata", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CHANNEL_METADATA_DELETE,
        operation_object_code="OBJ_CHANNEL", operation_object_params={"name": meta_name},
        operation_content_code="log.metadata.delete",
        content_id=content_id,
        entity_type="channel_metadata",
        entity_id=old.id if old else None,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


# ═══════════════════════════════════════════════════════════
# Schedule 元数据 (SCHEDULE)
# ═══════════════════════════════════════════════════════════

@router.get("/{content_id}/schedule", response_model=ScheduleMetadataItem | None)
async def get_schedule_metadata(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询 Schedule 元数据。"""
    return await metadata_service.get_schedule_metadata(db, content_id)


@router.post("/{content_id}/schedule", response_model=ScheduleMetadataItem)
async def create_schedule_metadata(
    content_id: int,
    data: ScheduleMetadataCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建 Schedule 元数据。"""
    data_dict = data.model_dump()
    data_dict["content_id"] = content_id
    result = await metadata_service.create_schedule_metadata(
        db, ScheduleMetadataCreate(**data_dict), processed_by=current_user.username
    )
    new_data = orm_to_dict(result, "schedule_metadata")
    # Custom Tags 存于 content 主表关联（schedule_metadata 表无此列，弹窗保存时经 updateContent 单独提交），
    # 日志快照补记名称列表，供 Activity Log 展示（bug 32430）
    new_data["custom_tag_names"] = await _load_custom_tag_names(db, content_id)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "schedule_metadata", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_METADATA_CREATE,
        operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": result.name},
        operation_content_code="log.metadata.create",
        content_id=content_id,
        entity_type="schedule_metadata",
        entity_id=result.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.put("/{content_id}/schedule", response_model=ScheduleMetadataItem)
async def update_schedule_metadata(
    content_id: int,
    data: ScheduleMetadataUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新 Schedule 元数据。"""
    import copy
    old = await metadata_service.get_schedule_metadata(db, content_id)
    old_data = copy.deepcopy(orm_to_dict(old, "schedule_metadata")) if old else {}
    result = await metadata_service.update_schedule_metadata(db, content_id, data, processed_by=current_user.username)
    new_data = orm_to_dict(result, "schedule_metadata")
    # Custom Tags 存于 content 主表关联，prev/new 快照补记同一名称列表：
    # 本接口不产生 tags 变更（由 updateContent 单独提交），补齐后 diff 不产生误报（bug 32430）
    tag_names = await _load_custom_tag_names(db, content_id)
    if old_data:
        old_data["custom_tag_names"] = tag_names
    new_data["custom_tag_names"] = tag_names
    prev_val, upd_val, raw_val = await prepare_log_values(db, "schedule_metadata", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.SCHEDULE_METADATA_UPDATE,
            operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": result.name},
            operation_content_code="log.metadata.edit",
            content_id=content_id,
            entity_type="schedule_metadata",
            entity_id=result.id,
            previous_value=prev_val,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{content_id}/schedule")
async def delete_schedule_metadata(
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除 Schedule 元数据。"""
    old = await metadata_service.get_schedule_metadata(db, content_id)
    old_data = orm_to_dict(old, "schedule_metadata") if old else {}
    meta_name = old.name if old else f"ID={content_id}"
    await metadata_service.delete_schedule_metadata(db, content_id)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "schedule_metadata", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_METADATA_DELETE,
        operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": meta_name},
        operation_content_code="log.metadata.delete",
        content_id=content_id,
        entity_type="schedule_metadata",
        entity_id=old.id if old else None,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}
