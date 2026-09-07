"""
直播管理 API 路由层。

路由前缀：/live

接口列表：
- 频道管理：
    GET    /channels                    查询频道列表（分页 + 多维过滤）
    GET    /channels/{channel_id}       查询频道详情
    PUT    /channels/{channel_id}       编辑频道
    DELETE /channels/{channel_id}       软删除频道（待实现）

- 物理频道：
    GET    /channels/{channel_id}/physical-channels         查询物理频道列表
    POST   /channels/{channel_id}/physical-channels         新增物理频道
    PUT    /channels/{channel_id}/physical-channels/{pc_id} 编辑物理频道
    DELETE /channels/{channel_id}/physical-channels/{pc_id} 删除物理频道

- 内容关联：
    GET    /contents/{content_id}/packages              查询内容-服务包关联
    POST   /contents/{content_id}/packages              新增内容-服务包关联
    DELETE /contents/{content_id}/packages/{package_id} 删除内容-服务包关联
    GET    /contents/{content_id}/categories            查询内容-栏目关联
    POST   /contents/{content_id}/categories            新增内容-栏目关联
    DELETE /contents/{content_id}/categories/{category_id} 删除内容-栏目关联

- 流程/日志：
    GET    /contents/{content_id}/processes             查询流程列表
    GET    /contents/{content_id}/status-logs           查询状态日志
    GET    /contents/{content_id}/activity-logs         查询活动日志

- 节目单管理：
    GET    /schedules                   查询节目单列表（分页 + 多维过滤）
    POST   /schedules                   新增节目单
    DELETE /schedules/{schedule_id}     软删除节目单

- 归档管理：
    GET    /archives                    查询归档内容列表（分页 + 多维过滤）
"""

import io

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values, resolve_option_display_name
from app.internal.cms_biz_system.models.user import User
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_orchestration.schemas.live import (
    ChannelListItem, ChannelDetailItem, ChannelUpdate,
    ScheduleListItem, ScheduleCreate, ScheduleImportResult,
    ArchiveListItem, ArchiveRequest, ArchiveResponse,
    PhysicalChannelListItem, PhysicalChannelCreate,
    PhysicalChannelHistoryItem,
    ContentPackageRef, ContentCategoryRef, ContentPackageLink, ContentCategoryLink,
    ProcessListItem, StatusLogListItem, ActivityLogListItem,
    ReviewRequest, ReviewResponse,
)
from app.internal.cms_biz_metada.schemas.basic import EntityFieldValueItem, EntityFieldValuesPayload
from app.internal.cms_biz_metada.services.entity_data_service import get_field_values, save_field_values
from app.internal.cms_biz_orchestration.services import live_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log

router = APIRouter(prefix="/live")


# ─── 频道管理 ─────────────────────────────────────────────────────────

@router.get("/channels", response_model=PaginatedResponse[ChannelListItem])
async def get_channel_list(
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    statuses: list[str] | None = Query(default=None),
    genre_ids: list[int] | None = Query(default=None),
    provider_id: int | None = None,
    provider_ids: list[int] | None = Query(default=None),
    package_name: str | None = None,
    package_id: int | None = None,
    package_ids: list[int] | None = Query(default=None),
    category_id: int | None = None,
    category_name: str | None = None,
    custom_tag_ids: list[int] | None = Query(default=None),
    channel_number: str | None = None,
    languages: list[str] | None = Query(default=None),
    license_start_from: str | None = None,
    license_start_to: str | None = None,
    license_end_from: str | None = None,
    license_end_to: str | None = None,
    publish_date_from: str | None = None,
    publish_date_to: str | None = None,
    unpublish_date_from: str | None = None,
    unpublish_date_to: str | None = None,
    is_discarded: bool | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await live_service.list_channels(
        db=db,
        page=page,
        page_size=page_size,
        title=title,
        statuses=statuses,
        genre_ids=genre_ids,
        provider_id=provider_id,
        provider_ids=provider_ids,
        package_name=package_name,
        package_id=package_id,
        package_ids=package_ids,
        category_id=category_id,
        category_name=category_name,
        custom_tag_ids=custom_tag_ids,
        channel_number=channel_number,
        languages=languages,
        license_start_from=license_start_from,
        license_start_to=license_start_to,
        license_end_from=license_end_from,
        license_end_to=license_end_to,
        publish_date_from=publish_date_from,
        publish_date_to=publish_date_to,
        unpublish_date_from=unpublish_date_from,
        unpublish_date_to=unpublish_date_to,
        is_discarded=is_discarded,
        sort_by=sort_by,
        sort_order=sort_order,
        current_user=current_user,
    )


@router.get("/channels/{channel_id}", response_model=ChannelDetailItem)
async def get_channel_detail(
    channel_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await live_service.get_channel(db, channel_id)


@router.put("/channels/{channel_id}", response_model=ChannelDetailItem)
async def update_channel_api(
    channel_id: int,
    body: ChannelUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await live_service.get_channel(db, channel_id)
    old_data = orm_to_dict(old, "content")
    channel = await live_service.update_channel(db, channel_id, body, processed_by=current_user.username)
    new_data = orm_to_dict(channel, "content")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "content", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CHANNEL_UPDATE,
        operation_object_code="OBJ_CHANNEL", operation_object_params={"name": old.title},
        operation_content_code="log.channel.edit",
        content_id=channel_id,
        entity_type="content",
        entity_id=channel_id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return channel


# ─── 物理频道 ───────────────────────────────────────────────────────────

async def _load_custom_field_display_data(db: AsyncSession, entity_type: str, entity_id: int) -> dict[str, str]:
    """读取实体已保存的自定义字段值并转为展示值（field_name → 展示值）。

    自定义字段值存于 entity_field_value 多态表（不在实体表列中），创建/删除日志
    快照需单独合并（bug 32430）。下拉/多选字段的选项 code 经 resolve_option_display_name
    翻译为人类可读名称，与创建弹窗展示口径一致。
    """
    from sqlalchemy import select

    from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption, EntityFieldValue

    value_rows = (await db.execute(
        select(EntityFieldValue).where(
            EntityFieldValue.entity_type == entity_type,
            EntityFieldValue.entity_id == entity_id,
            EntityFieldValue.is_deleted.is_(False),
        )
    )).scalars().all()
    value_rows = [r for r in value_rows if r.value is not None and r.value != ""]
    if not value_rows:
        return {}

    field_ids = [r.custom_field_id for r in value_rows]
    field_info = {
        cf.id: cf for cf in (
            await db.execute(
                select(CustomField).where(CustomField.id.in_(field_ids), CustomField.is_deleted.is_(False))
            )
        ).scalars().all()
    }

    dropdown_ids = [
        cf.id for cf in field_info.values()
        if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select')
    ]
    option_map: dict[int, dict[str, str]] = {}
    if dropdown_ids:
        opt_rows = (await db.execute(
            select(CustomFieldOption).where(CustomFieldOption.custom_field_id.in_(dropdown_ids))
        )).scalars().all()
        for opt in opt_rows:
            option_map.setdefault(opt.custom_field_id, {})[opt.code] = resolve_option_display_name(opt.names or {}, opt.code)

    data: dict[str, str] = {}
    for r in value_rows:
        cf = field_info.get(r.custom_field_id)
        if cf is None:
            continue
        if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select') and r.custom_field_id in option_map:
            data[cf.field_name] = ','.join(
                option_map[r.custom_field_id].get(c.strip(), c.strip()) for c in r.value.split(',')
            )
        else:
            data[cf.field_name] = r.value
    return data


@router.get("/channels/{channel_id}/physical-channels", response_model=PaginatedResponse[PhysicalChannelListItem])
async def list_physical_channels_api(
    channel_id: int,
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await live_service.list_physical_channels(db, channel_id, page, page_size)


@router.post("/channels/{channel_id}/physical-channels", response_model=PhysicalChannelListItem)
async def create_physical_channel_api(
    channel_id: int,
    body: PhysicalChannelCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pc = await live_service.create_physical_channel(db, channel_id, body, current_user.username)
    # 自定义字段随新增一并保存，并合并进同一条 Add 日志（避免单独记录一条 Update 日志）
    if body.custom_fields:
        await save_field_values(db, "PhysicalChannel", pc.id, EntityFieldValuesPayload(values=body.custom_fields))
    # 值刚入库，统一从 entity_field_value 读取展示值（与删除日志同口径，bug 32430）
    custom_field_data = await _load_custom_field_display_data(db, "PhysicalChannel", pc.id)

    new_data = {k: v for k, v in body.model_dump(exclude={'custom_fields'}).items() if v is not None}
    new_data.update(custom_field_data)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "physical_channel", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PHYSICAL_CHANNEL_CREATE,
        operation_object_code="OBJ_PHYSICAL_CHANNEL", operation_object_params={"name": pc.name},
        operation_content_code="log.physicalChannel.create",
        content_id=channel_id,
        entity_type="physical_channel",
        entity_id=pc.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return pc


@router.delete("/channels/{channel_id}/physical-channels/{pc_id}")
async def delete_physical_channel_api(
    channel_id: int,
    pc_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_physical_channel_by_id
    pc_obj = await get_physical_channel_by_id(db, pc_id)
    pc_name = pc_obj.name if pc_obj else f"ID={pc_id}"
    old_data = orm_to_dict(pc_obj, "physical_channel") if pc_obj else {}
    # 自定义字段值存于 entity_field_value 多态表，补记进删除日志快照，与创建日志口径一致（bug 32430）
    old_data.update(await _load_custom_field_display_data(db, "PhysicalChannel", pc_id))
    await live_service.delete_physical_channel(db, channel_id, pc_id, current_user.username)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "physical_channel", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PHYSICAL_CHANNEL_DELETE,
        operation_object_code="OBJ_PHYSICAL_CHANNEL", operation_object_params={"name": pc_name},
        operation_content_code="log.physicalChannel.delete",
        content_id=channel_id,
        entity_type="physical_channel",
        entity_id=pc_id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/channels/{channel_id}/physical-channels/history", response_model=PaginatedResponse[PhysicalChannelHistoryItem])
async def list_physical_channel_history_api(
    channel_id: int,
    page: int = 1,
    page_size: int = 10,
    processed_type: str | None = None,
    processed_by: str | None = None,
    processed_at_from: str | None = None,
    processed_at_to: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询物理频道操作历史记录"""
    return await live_service.list_physical_channel_history(db, channel_id, page, page_size, processed_type, processed_by, processed_at_from, processed_at_to)


@router.get("/channels/{channel_id}/physical-channels/{pc_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_physical_channel_field_values_api(
    channel_id: int,
    pc_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询物理频道自定义字段值"""
    return await get_field_values(db, "PhysicalChannel", pc_id)


@router.put("/channels/{channel_id}/physical-channels/{pc_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_physical_channel_field_values_api(
    channel_id: int,
    pc_id: int,
    body: EntityFieldValuesPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """保存物理频道自定义字段值"""
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_physical_channel_by_id
    from app.internal.cms_biz_metada.services.entity_data_service import get_field_values
    from app.internal.cms_biz_metada.models.basic import CustomField
    from sqlalchemy import select

    # 获取物理频道信息
    pc_obj = await get_physical_channel_by_id(db, pc_id)
    pc_name = pc_obj.name if pc_obj else f"ID={pc_id}"

    # 获取旧的自定义字段值
    old_field_values = await get_field_values(db, "PhysicalChannel", pc_id)
    old_data = {f"custom_field_{fv.custom_field_id}": fv.value for fv in old_field_values}

    # 获取自定义字段信息（用于构建字段名映射）
    custom_field_ids = [item.custom_field_id for item in body.values if item.custom_field_id]
    custom_field_map = {}
    if custom_field_ids:
        cf_result = await db.execute(
            select(CustomField).where(CustomField.id.in_(custom_field_ids), CustomField.is_deleted.is_(False))
        )
        for cf in cf_result.scalars().all():
            custom_field_map[cf.id] = cf.field_name

    # 保存新的自定义字段值
    result = await save_field_values(db, "PhysicalChannel", pc_id, body)

    # 构建新的数据（包含字段名）
    new_data = {}
    for item in result:
        field_name = custom_field_map.get(item.custom_field_id, f"custom_field_{item.custom_field_id}")
        new_data[field_name] = item.value

    # 构建旧数据（使用相同的字段名）
    old_data_named = {}
    for fv in old_field_values:
        field_name = custom_field_map.get(fv.custom_field_id, f"custom_field_{fv.custom_field_id}")
        old_data_named[field_name] = fv.value

    # 记录操作日志
    prev_val, upd_val, raw_val = await prepare_log_values(db, "physical_channel", old_data_named, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CHANNEL_FIELD_UPDATE,
        operation_object_code="OBJ_PHYSICAL_CHANNEL", operation_object_params={"name": pc_name},
        operation_content_code="log.field.edit",
        content_id=channel_id,
        entity_type="physical_channel",
        entity_id=pc_id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


# ─── 内容-服务包关联 ───────────────────────────────────────────────────

@router.get("/contents/{content_id}/packages", response_model=list[ContentPackageRef])
async def list_content_packages_api(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await live_service.list_content_packages(db, content_id)


@router.post("/contents/{content_id}/packages", response_model=list[ContentPackageRef])
async def link_content_packages_api(
    content_id: int,
    body: ContentPackageLink,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json
    before_packages = await live_service.list_content_packages(db, content_id)
    before_name_set = {p.name for p in before_packages if p.name}
    result = await live_service.link_content_packages(db, content_id, body.package_ids, current_user.username)
    after_packages = await live_service.list_content_packages(db, content_id)
    after_name_set = {p.name for p in after_packages if p.name}
    added_names = ",".join(sorted(after_name_set - before_name_set))
    upd_val = json.dumps({"package_names": added_names}, ensure_ascii=False) if added_names else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_PACKAGE_LINK,
        operation_object_code="log.package.link",
        operation_content_code="log.package.link",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        # 原始入参快照：关联的服务包 ID 列表
        updated_value_json=json.dumps({"content_id": content_id, "package_ids": body.package_ids}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/contents/{content_id}/packages/{package_id}")
async def unlink_content_package_api(
    content_id: int,
    package_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json
    before_packages = await live_service.list_content_packages(db, content_id)
    before_name_set = {p.name for p in before_packages if p.name}
    await live_service.unlink_content_package(db, content_id, package_id, current_user.username)

    # 已发布内容取消关联后回滚状态
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
    from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
    content = await get_content_by_id(db, content_id)
    if content:
        await rollback_after_published_edit(
            db, content_id, content.content_type,
            current_user.username, "取消服务包关联",
        )

    after_packages = await live_service.list_content_packages(db, content_id)
    after_name_set = {p.name for p in after_packages if p.name}
    removed_names = ",".join(sorted(before_name_set - after_name_set))
    prev_val = json.dumps({"package_names": removed_names}, ensure_ascii=False) if removed_names else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_PACKAGE_UNLINK,
        operation_object_code="log.package.unlink",
        operation_content_code="log.package.unlink",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=prev_val,
        updated_value=None,
        # 原始入参快照：被解除关联的服务包 ID
        updated_value_json=json.dumps({"content_id": content_id, "package_id": package_id}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


# ─── 内容-栏目关联 ───────────────────────────────────────────────────────

@router.get("/contents/{content_id}/categories", response_model=list[ContentCategoryRef])
async def list_content_categories_api(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await live_service.list_content_categories(db, content_id)


@router.post("/contents/{content_id}/categories", response_model=list[ContentCategoryRef])
async def link_content_categories_api(
    content_id: int,
    body: ContentCategoryLink,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json
    before_categories = await live_service.list_content_categories(db, content_id)
    before_name_set = {c.name for c in before_categories if c.name}
    result = await live_service.link_content_categories(db, content_id, body.category_ids, current_user.username)
    after_categories = await live_service.list_content_categories(db, content_id)
    after_name_set = {c.name for c in after_categories if c.name}
    added_names = ",".join(sorted(after_name_set - before_name_set))
    upd_val = json.dumps({"category_names": added_names}, ensure_ascii=False) if added_names else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_CATEGORY_LINK,
        operation_object_code="log.category.link",
        operation_content_code="log.category.link",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        # 原始入参快照：关联的栏目 ID 列表
        updated_value_json=json.dumps({"content_id": content_id, "category_ids": body.category_ids}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/contents/{content_id}/categories/{category_id}")
async def unlink_content_category_api(
    content_id: int,
    category_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json
    before_categories = await live_service.list_content_categories(db, content_id)
    before_name_set = {c.name for c in before_categories if c.name}
    await live_service.unlink_content_category(db, content_id, category_id)

    # 已发布内容取消关联后回滚状态
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
    from app.internal.cms_biz_orchestration.services.workflow_service import (
        rollback_after_published_edit,
        complete_process_and_update_status,
    )
    content = await get_content_by_id(db, content_id)
    if content:
        await rollback_after_published_edit(
            db, content_id, content.content_type,
            current_user.username, "取消栏目关联",
        )
        # 取消栏目关联补写 Category 流程记录（与关联栏目对称）：
        # check_category 评估为 Pending → Processes 显示红x；
        # skip_status_update 与删除海报口径一致，仅补记录不推进状态
        await complete_process_and_update_status(
            db,
            content_id=content_id,
            content_type=content.content_type,
            process_name="Category",
            processed_by=current_user.username,
            info=f"删除栏目关联: category_id={category_id}",
            skip_status_update=True,
        )

    after_categories = await live_service.list_content_categories(db, content_id)
    after_name_set = {c.name for c in after_categories if c.name}
    removed_names = ",".join(sorted(before_name_set - after_name_set))
    prev_val = json.dumps({"category_names": removed_names}, ensure_ascii=False) if removed_names else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_CATEGORY_UNLINK,
        operation_object_code="log.category.unlink",
        operation_content_code="log.category.unlink",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=prev_val,
        updated_value=None,
        # 原始入参快照：被解除关联的栏目 ID
        updated_value_json=json.dumps({"content_id": content_id, "category_id": category_id}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


# ─── 流程/日志 ───────────────────────────────────────────────────────────

@router.get("/contents/{content_id}/processes", response_model=list[ProcessListItem])
async def list_processes_api(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await live_service.list_processes(db, content_id)
    await db.commit()
    return result


@router.get("/contents/{content_id}/review-status", response_model=dict)
async def get_content_review_status(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询内容的审核状态，用于前端判断申请是否已发起。
    
    返回：
        - has_initiated_review: 是否已发起审核申请
        - has_review: 审核是否已通过
        - review_status: 当前审核状态
    """
    result = await live_service.get_review_status(db, content_id)
    return result


@router.get("/contents/{content_id}/status-logs", response_model=list[StatusLogListItem])
async def list_status_logs_api(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await live_service.list_status_logs(db, content_id)


@router.get("/contents/{content_id}/activity-logs", response_model=list[ActivityLogListItem])
async def list_activity_logs_api(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await live_service.list_activity_logs(db, content_id)


# ─── 节目单管理 ───────────────────────────────────────────────────────

@router.get("/schedules", response_model=PaginatedResponse[ScheduleListItem])
async def get_schedule_list(
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    channel_id: int | None = None,
    channel_name: str | None = None,
    cutv_enable: bool | None = None,
    cutv_enables: list[str] | None = Query(default=None),
    is_archived: bool | None = None,
    is_deleted: bool | None = None,
    is_discarded: bool | None = None,
    statuses: list[str] | None = Query(default=None),
    begin_from: str | None = None,
    begin_to: str | None = None,
    end_from: str | None = None,
    end_to: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await live_service.list_schedules(
        db=db,
        page=page,
        page_size=page_size,
        title=title,
        channel_id=channel_id,
        channel_name=channel_name,
        cutv_enable=cutv_enable,
        cutv_enables=cutv_enables,
        is_archived=is_archived,
        is_deleted=is_deleted,
        is_discarded=is_discarded,
        statuses=statuses,
        begin_from=begin_from,
        begin_to=begin_to,
        end_from=end_from,
        end_to=end_to,
        sort_by=sort_by,
        sort_order=sort_order,
        current_user=current_user,
    )


@router.get("/schedules/template")
async def download_schedule_import_template(
    _: User = Depends(get_current_user),
):
    """下载节目单导入模板。

    模板包含可导入字段（必填字段表头带红色 (*) 标记）、两行示例数据及填写说明（Instructions 工作表）。
    """
    from openpyxl import Workbook
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = Workbook()
    ws = wb.active
    ws.title = "Schedules"

    # 必填字段表头带红色 (*) 标记（导入时按表头名称匹配，(*) 会被忽略）
    # 24 列：Status 与 CUTV Enable 已从导入模板移除
    # （CUTV Enable 导入后默认为 NO；Status 由业务流程变更，均不可通过导入设置）
    headers = [
        "Content ID", "Program Name(*)", "Channel Name(*)", "Begin Time(*)", "End Time(*)",
        "BroadcastType", "RatingLevel(*)", "Advice",
        "SectionsInfo", "Description", "Audio Lang", "Subtitle Lang",
        "TSTV Enable", "TSTV Mode",
        "NPVR Enable", "PPV Enable", "Pre Buffer", "Post Buffer",
        "Purchase Begin Time", "Purchase End Time",
        "Genre", "Package", "Custom Tags", "StatusFlag",
    ]
    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    # 富文本字体：字段名白色 + (*) 红色（写在同一单元格）
    base_inline = InlineFont(rFont="Calibri", b=True, color="FFFFFF")
    mark_inline = InlineFont(rFont="Calibri", b=True, color="FF0000")

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col)
        if header.endswith("(*)"):
            cell.value = CellRichText(
                TextBlock(base_inline, header[:-3]),
                TextBlock(mark_inline, "(*)"),
            )
        else:
            cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 示例数据行
    example_rows = [
        # 示例 1：PPV Enable = YES → Pre Buffer / Post Buffer / Package 必填
        [
            "", "Evening News", "Channel A", "2025-01-01 18:00:00", "2025-01-01 19:00:00",
            "first", "PG", "Violence",
            "[{\"type\": 3, \"action\": 0, \"tag\": \"news\", \"start\": 0, \"end\": 1800}]",
            "Daily evening news program", "English", "English",
            "YES", "NO",
            "YES", "YES", "0", "0",
            "180", "-1",
            "News", "Basic Package", "Tag1,Tag2", "YES",
        ],
        # 示例 2：PPV Enable = NO → Pre Buffer / Post Buffer / Package 可留空
        [
            "", "Late Night Talk", "Channel A", "2025-01-01 20:00:00", "2025-01-01 21:00:00",
            "first", "PG", "",
            "", "Late night talk show",
            "English", "English",
            "YES", "NO",
            "YES", "NO", "", "",
            "", "",
            "News", "", "", "YES",
        ],
    ]
    for row_idx, example_data in enumerate(example_rows, 2):
        for col_idx, value in enumerate(example_data, 1):
            ws.cell(row=row_idx, column=col_idx, value=value)

    # Content ID 示例说明（英文批注）：存在 → 更新，留空 → 新增
    from openpyxl.comments import Comment
    ws.cell(row=2, column=1).comment = Comment(
        "Content ID: If the ID already exists, that schedule will be updated.",
        "CMS", height=60, width=260,
    )
    ws.cell(row=3, column=1).comment = Comment(
        "Content ID: Leave empty to create a new schedule.",
        "CMS", height=60, width=260,
    )

    # 设置列宽
    col_widths = [12, 30, 24, 22, 22,
                  16, 16, 16,
                  30, 30, 16, 16,
                  14, 14,
                  14, 14, 12, 12, 20, 20,
                  20, 24, 20, 14]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    # 填写说明工作表（仅英文）
    ws_notes = wb.create_sheet("Instructions")
    notes = [
        "Instructions",
        "",
        "1. Fields marked with a red (*) are required: Program Name, Channel Name, Begin Time, End Time, RatingLevel. "
        "If any row is missing a required field, the entire file will be rejected.",
        "2. Content ID: If the ID already exists, that schedule will be updated (overwritten with the latest data); "
        "if left empty, a new schedule will be created.",
        "3. When PPV Enable is YES, Pre Buffer, Post Buffer and Package are required; "
        "they can be left empty when PPV Enable is NO.",
        "4. Package: fill in the package name (not the ID); separate multiple values with commas. "
        "Names must match existing packages in the system.",
        "5. Enable fields (TSTV/NPVR/PPV Enable, StatusFlag): YES or NO.",
        "6. Time fields format: YYYY-MM-DD HH:MM:SS (e.g. 2025-01-01 18:00:00).",
        "7. BroadcastType/RatingLevel/Advice/Audio Lang/Subtitle Lang/Genre/Custom Tags must match existing data "
        "in the system; rows that fail to match will be skipped.",
        "8. SectionsInfo is a JSON array, e.g. [{\"type\": 3, \"action\": 0, \"tag\": \"news\", \"start\": 0, \"end\": 1800}]. "
        "type: 1=intro/2=ad/3=chapter; action: 0=no skip/1=skip; tag is the label text; start/end are integer seconds.",
        "9. CUTV Enable is not importable: it always defaults to NO for imported schedules. "
        "Status is not importable either: it is managed by business workflows.",
    ]
    for row_idx, note in enumerate(notes, 1):
        ws_notes.cell(row=row_idx, column=1, value=note)
    ws_notes.column_dimensions["A"].width = 120
    ws_notes["A1"].font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Schedule_Import_Template.xlsx"},
    )


@router.get("/schedules/{schedule_id}", response_model=ScheduleListItem)
async def get_schedule_api(
    schedule_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await live_service.get_schedule(db, schedule_id)


@router.post("/schedules", response_model=ScheduleListItem)
async def create_schedule_api(
    body: ScheduleCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    schedule = await live_service.create_schedule(db, body)

    # 新增节目单属于父级（频道）节点数据变更，回退祖先状态
    if schedule.channel_id:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_ancestors_after_child_change
        await rollback_ancestors_after_child_change(
            db,
            start_parent_id=schedule.channel_id,
            edited_by=current_user.username,
            edit_info=f"新增节目单「{schedule.title}」",
        )

    new_data = orm_to_dict(schedule, "schedule")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "schedule", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_CREATE,
        operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": schedule.title},
        operation_content_code="log.metadata.create",
        content_id=schedule.id,
        entity_type="schedule",
        entity_id=schedule.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return schedule


@router.delete("/schedules/{schedule_id}")
async def delete_schedule_api(
    schedule_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    schedule = await live_service.get_schedule(db, schedule_id)
    schedule_title = schedule.title
    # 删除前记录所属频道（父级），删除后用于回退祖先状态
    schedule_channel_id = schedule.channel_id
    old_data = orm_to_dict(schedule, "schedule")
    await live_service.delete_schedule(db, schedule_id)

    # 删除节目单属于父级（频道）节点数据变更，回退祖先状态
    if schedule_channel_id:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_ancestors_after_child_change
        await rollback_ancestors_after_child_change(
            db,
            start_parent_id=schedule_channel_id,
            edited_by=current_user.username,
            edit_info=f"删除节目单「{schedule_title}」",
        )

    prev_val, upd_val, raw_val = await prepare_log_values(db, "schedule", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_DELETE,
        operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": schedule_title},
        operation_content_code="log.metadata.delete",
        content_id=schedule_id,
        entity_type="schedule",
        entity_id=schedule_id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.post("/schedules/export")
async def export_schedules_api(
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from datetime import datetime

    ids = body.get("ids", [])
    data = await live_service.export_schedules_excel(db, ids)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_BATCH_EXPORT,
        operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": ids},
        operation_content_code="LOG_SCHEDULE_BATCH_EXPORT", operation_content_params={"ids": ids},
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=schedules_{timestamp}.xlsx"},
    )


@router.post("/schedules/import", response_model=ScheduleImportResult)
async def import_schedules_api(
    file: UploadFile,
    request: Request,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 提前读取当前用户信息，避免长耗时导入后 ORM 对象过期导致 MissingGreenlet
    user_id = current_user.id
    user_name = current_user.username
    result = await live_service.import_schedules_excel(db, file, force=force, processed_by=user_name)
    # 冲突未覆盖时不记录业务导入日志
    if result.conflicts and not force:
        return result
    await write_log(
        db,
        user_id=user_id,
        user_name=user_name,
        operation_type=OperationType.SCHEDULE_IMPORT,
        operation_object_code="OBJ_SCHEDULE",
        operation_content_code="LOG_SCHEDULE_IMPORT", operation_content_params={"total": result.total, "created": result.created},
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


# ─── 归档管理 ─────────────────────────────────────────────────────────

@router.get("/archives", response_model=PaginatedResponse[ArchiveListItem])
async def get_archive_list(
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    genre_ids: list[int] | None = Query(default=None),
    provider_ids: list[int] | None = Query(default=None),
    package_ids: list[int] | None = Query(default=None),
    category_id: int | None = None,
    custom_tag_ids: list[int] | None = Query(default=None),
    deleted: str | None = None,
    type_ids: list[int] | None = Query(default=None),
    channel_name: str | None = None,
    program_name: str | None = None,
    begin_time_from: str | None = None,
    begin_time_to: str | None = None,
    end_time_from: str | None = None,
    end_time_to: str | None = None,
    license_start_from: str | None = None,
    license_start_to: str | None = None,
    license_end_from: str | None = None,
    license_end_to: str | None = None,
    source_schedule_id: int | None = None,
    publish_date_from: str | None = None,
    publish_date_to: str | None = None,
    unpublish_date_from: str | None = None,
    unpublish_date_to: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await live_service.list_archives(
        db, page, page_size, title, content_types, statuses, genre_ids,
        provider_ids, package_ids, category_id, custom_tag_ids, deleted, type_ids, channel_name,
        program_name, begin_time_from, begin_time_to, end_time_from, end_time_to,
        license_start_from, license_start_to,
        license_end_from, license_end_to,
        source_schedule_id,
        publish_date_from, publish_date_to,
        unpublish_date_from, unpublish_date_to,
        sort_by, sort_order,
        current_user=current_user,
    )


# ─── 归档操作 ───────────────────────────────────────────────────────────

@router.post("/schedules/archive", response_model=ArchiveResponse)
async def archive_schedule_api(
    body: ArchiveRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """归档节目单：根据 SeriesType 创建 MOVIE/EPISODE/SERIES/SEASON 归档产物"""
    result = await live_service.archive_schedule(db, body, processed_by=current_user.username)
    import json as _json
    # 归档日志补充节目单标题与所属频道（此前 name 传的是 int 型 id）
    schedule = await live_service.get_schedule(db, body.schedule_id)
    channel_title = None
    if schedule.channel_id:
        from sqlalchemy import select
        from app.internal.cms_biz_package.models.package import Content
        channel_title = (
            await db.execute(select(Content.title).where(Content.id == schedule.channel_id))
        ).scalar_one_or_none()
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_ARCHIVE,
        operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": schedule.title},
        operation_content_code="LOG_SCHEDULE_ARCHIVE",
        operation_content_params={"name": schedule.title, "channel": channel_title or ""},
        content_id=body.schedule_id,
        # 原始入参 + 归档产物快照（title/channel_name 与前端 schedule 标签段对齐）
        updated_value_json=_json.dumps({
            "title": schedule.title,
            "channel_id": schedule.channel_id,
            "channel_name": channel_title,
            "archive_content_id": result.archive_content_id,
            "archive_content_type": result.archive_content_type,
        }, ensure_ascii=False, default=str),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


# ─── 归档管理 Excel 导出/导入 ───────────────────────────────────────────

@router.post("/archives/export")
async def export_archives_api(
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导出归档内容为 Excel 文件（含全部字段及关联信息）。"""
    from datetime import datetime

    ids = body.get("ids", [])
    data = await live_service.export_archives_excel(db, ids)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_BATCH_EXPORT,
        operation_object_code="OBJ_ARCHIVE", operation_object_params={"name": ids},
        operation_content_code="LOG_CONTENT_BATCH_EXPORT", operation_content_params={"ids": ids},
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=archives_{timestamp}.xlsx"},
    )


@router.post("/archives/import", response_model=live_service.ArchiveImportResult)
async def import_archives_api(
    file: UploadFile,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """从 Excel 批量归档节目单（36 列模板 = VOD 35 列 + 节目单 ID 1 列）。

    Content ID（归档内容 ID）有值→仅更新元数据；否则 Schedule ID（节目单 ID）
    定位待归档节目单执行归档。行级失败仅回滚该行。
    """
    result = await live_service.import_archives_excel(db, file, processed_by=current_user.username)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_BATCH_IMPORT,
        operation_object_code="OBJ_ARCHIVE",
        operation_content_code="LOG_CONTENT_BATCH_IMPORT", operation_content_params={"result": f"total={result.total}, created={result.created}, updated={result.updated}, skipped={result.skipped}"},
        ip_address=_get_ip(request),
        result="success" if not result.errors else "partial",
    )
    await db.commit()
    return result


@router.get("/archives/template")
async def download_archive_import_template(
    _: User = Depends(get_current_user),
):
    """下载归档导入模板（36 列 = VOD 导入模板 35 列 + 节目单 ID 1 列，含示例与填写说明）。"""
    content = live_service.generate_archive_import_template()
    return StreamingResponse(
        iter([content]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=Archive_Import_Template.xlsx"},
    )


# ─── 审核管理 ───────────────────────────────────────────────────────────

@router.post("/contents/{content_id}/review/initiate", response_model=ReviewResponse)
async def initiate_content_review_api(
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """发起内容审核（内容编辑点击"发起审核"按钮）
    
    - 免审批：自动通过并创建发布任务
    - 需审批：创建审批记录，等待审批人审批
    """
    import json
    result = await live_service.initiate_content_review(
        db,
        content_id,
        current_user.username,
    )
    upd_val = json.dumps({
        "auto_approved": result.get("auto_approved", False),
        "review_level": result.get("review_level"),
    }, ensure_ascii=False, default=str)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_REVIEW_INITIATE,
        operation_object_code="log.review.initiate",
        operation_content_code="log.review.initiate",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        # 原始入参快照
        updated_value_json=json.dumps({"content_id": content_id}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return ReviewResponse(**result)


@router.post("/contents/{content_id}/review", response_model=ReviewResponse)
async def submit_content_review_api(
    content_id: int,
    body: ReviewRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """提交内容审核（审批人操作）"""
    import json
    result = await live_service.submit_content_review(
        db,
        content_id,
        body.review_type,
        body.issue_types,
        body.description,
        current_user.username,
        body.review_level,
    )
    is_approve = body.review_type == "approve"
    op_type = OperationType.CONTENT_REVIEW_APPROVE if is_approve else OperationType.CONTENT_REVIEW_REJECT
    reject_reason = body.description if not is_approve and body.description else None
    upd_val_dict = {
        "review_type": body.review_type,
        "review_level": body.review_level,
    }
    if reject_reason:
        upd_val_dict["reason"] = reject_reason
    upd_val = json.dumps(upd_val_dict, ensure_ascii=False, default=str)
    op_content_kwargs = (
        {"operation_content_code": "log.review.approve"}
        if is_approve
        else {"operation_content_code": "log.review.reject", "operation_content_params": {"reason": reject_reason or ""}}
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=op_type,
        operation_object_code="log.review.initiate",
        **op_content_kwargs,
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        # 原始入参快照：审核提交参数
        updated_value_json=json.dumps({
            "content_id": content_id,
            "review_type": body.review_type,
            "issue_types": body.issue_types,
            "description": body.description,
            "review_level": body.review_level,
        }, ensure_ascii=False, default=str),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return ReviewResponse(**result)


@router.get("/contents/{content_id}/review/status")
async def get_content_review_status_api(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取内容审核状态，如果没有记录则返回空数据"""
    result = await live_service.get_content_review_status(db, content_id)
    if not result:
        # 返回空数据表示未发起审核
        return {
            "content_id": content_id,
            "review_level": None,
            "level_required": 0,
            "final_status": "None",
            "initiated_by": None,
            "initiated_at": None,
            "completed_at": None,
            "level_1_status": None,
            "level_1_by": None,
            "level_1_at": None,
            "level_1_comment": None,
            "level_2_status": None,
            "level_2_by": None,
            "level_2_at": None,
            "level_2_comment": None,
            "level_3_status": None,
            "level_3_by": None,
            "level_3_at": None,
            "level_3_comment": None,
        }
    return result


@router.get("/contents/{content_id}/review/permission")
async def check_content_review_permission_api(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """校验当前用户是否有权限进行内容审核操作"""
    result = await live_service.check_content_review_permission(db, content_id, current_user.username)
    return result