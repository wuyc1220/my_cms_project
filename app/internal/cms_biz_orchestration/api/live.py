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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
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
    genre_id: int | None = None,
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
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await live_service.list_channels(
        db, page, page_size, title, statuses, genre_id, genre_ids,
        provider_id, provider_ids, package_name, package_id, package_ids,
        category_id, category_name, custom_tag_ids,
        channel_number, languages,
        license_start_from, license_start_to,
        license_end_from, license_end_to,
        sort_by, sort_order,
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
    channel = await live_service.update_channel(db, channel_id, body)
    new_data = orm_to_dict(channel, "content")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "content", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CHANNEL_UPDATE,
        operation_object=f"频道 {old.title}",
        operation_content="log.channel.edit",
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
    new_data = {k: v for k, v in body.model_dump().items() if v is not None}
    prev_val, upd_val, raw_val = await prepare_log_values(db, "physical_channel", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PHYSICAL_CHANNEL_CREATE,
        operation_object=f"物理频道 {pc.name}",
        operation_content="log.physicalChannel.create",
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
    await live_service.delete_physical_channel(db, channel_id, pc_id, current_user.username)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "physical_channel", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PHYSICAL_CHANNEL_DELETE,
        operation_object=f"物理频道 {pc_name}",
        operation_content="log.physicalChannel.delete",
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
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询物理频道操作历史记录"""
    return await live_service.list_physical_channel_history(db, channel_id, page, page_size, processed_type, processed_by)


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
    result = await save_field_values(db, "PhysicalChannel", pc_id, body)
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
        operation_object="log.package.link",
        operation_content="log.package.link",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        updated_value_json=None,
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
    await live_service.unlink_content_package(db, content_id, package_id)
    after_packages = await live_service.list_content_packages(db, content_id)
    after_name_set = {p.name for p in after_packages if p.name}
    removed_names = ",".join(sorted(before_name_set - after_name_set))
    prev_val = json.dumps({"package_names": removed_names}, ensure_ascii=False) if removed_names else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_PACKAGE_UNLINK,
        operation_object="log.package.unlink",
        operation_content="log.package.unlink",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=None,
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
        operation_object="log.category.link",
        operation_content="log.category.link",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        updated_value_json=None,
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
    after_categories = await live_service.list_content_categories(db, content_id)
    after_name_set = {c.name for c in after_categories if c.name}
    removed_names = ",".join(sorted(before_name_set - after_name_set))
    prev_val = json.dumps({"category_names": removed_names}, ensure_ascii=False) if removed_names else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_CATEGORY_UNLINK,
        operation_object="log.category.unlink",
        operation_content="log.category.unlink",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=None,
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
        db, page, page_size, title, channel_id, channel_name, cutv_enable, cutv_enables, is_archived,
        begin_from, begin_to, end_from, end_to,
        sort_by, sort_order,
        current_user=current_user,
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
    new_data = orm_to_dict(schedule, "schedule")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "schedule", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_CREATE,
        operation_object=f"节目单 {schedule.title}",
        operation_content="log.metadata.create",
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
    old_data = orm_to_dict(schedule, "schedule")
    await live_service.delete_schedule(db, schedule_id)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "schedule", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_DELETE,
        operation_object=f"节目单 {schedule_title}",
        operation_content="log.metadata.delete",
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
    ids = body.get("ids", [])
    data = await live_service.export_schedules_excel(db, ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_BATCH_EXPORT,
        operation_object=f"节目单 {ids}",
        operation_content=f"批量导出节目单: {ids}",
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=schedules.xlsx"},
    )


@router.post("/schedules/import", response_model=ScheduleImportResult)
async def import_schedules_api(
    file: UploadFile,
    request: Request,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await live_service.import_schedules_excel(db, file, force=force)
    # 冲突未覆盖时不记录业务导入日志
    if result.conflicts and not force:
        return result
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SCHEDULE_IMPORT,
        operation_object="节目单导入",
        operation_content=f"Imported schedules: total={result.total}, created={result.created}, updated={result.updated}, force={force}",
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
    genre_id: int | None = None,
    provider_id: int | None = None,
    package_id: int | None = None,
    category_id: int | None = None,
    custom_tag_ids: list[int] | None = Query(default=None),
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
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await live_service.list_archives(
        db, page, page_size, title, content_types, statuses, genre_id,
        provider_id, package_id, category_id, custom_tag_ids, channel_name,
        program_name, begin_time_from, begin_time_to, end_time_from, end_time_to,
        license_start_from, license_start_to,
        license_end_from, license_end_to,
        source_schedule_id,
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
    result = await live_service.archive_schedule(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_EDIT,
        operation_object=f"节目单归档 ID={body.schedule_id}",
        operation_content=f"Archived schedule: ID={body.schedule_id}, archive_content_id={result.archive_content_id}, type={result.archive_content_type}",
        content_id=body.schedule_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


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
        operation_object="log.review.initiate",
        operation_content="log.review.initiate",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        updated_value_json=None,
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
    op_content = "log.review.approve" if is_approve else "log.review.reject"
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=op_type,
        operation_object="log.review.initiate",
        operation_content=op_content,
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=None,
        updated_value=upd_val,
        updated_value_json=None,
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