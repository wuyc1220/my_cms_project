"""
操作日志服务

对外暴露：
    write_log()   — 写入一条日志（供各路由埋点调用，内部捕获异常，不影响主业务）
    list_logs()   — 分页查询日志列表
    get_log()     — 获取单条日志详情
    clear_logs()  — 按时间范围删除日志
    export_logs_excel() — 导出 Excel（bytes）

操作类型常量集中定义在 OperationType 中，方便未来路由埋点直接引用。
"""

import io
from datetime import datetime

from loguru import logger
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.operation_log import OperationLog
from app.internal.cms_biz_system.schemas.operation_log import OperationLogItem
from app.common.schemas import PaginatedResponse
from app.common.core.exceptions import ErrorCode, NotFoundException
from app.common.core.i18n import get_msg

# ── 操作类型常量 ────────────────────────────────────────────
class OperationType:
    USER_LOGIN = "USER_LOGIN"
    USER_LOGOUT = "USER_LOGOUT"
    USER_CHANGE_PWD = "USER_CHANGE_PWD"

    USER_CREATE = "USER_CREATE"
    USER_EDIT = "USER_EDIT"
    USER_DELETE = "USER_DELETE"
    USER_RESET_PWD = "USER_RESET_PWD"
    USER_STATUS = "USER_STATUS"

    ROLE_CREATE = "ROLE_CREATE"
    ROLE_EDIT = "ROLE_EDIT"
    ROLE_DELETE = "ROLE_DELETE"
    ROLE_PERM = "ROLE_PERM"
    ROLE_STATUS = "ROLE_STATUS"

    DICT_CREATE = "DICT_CREATE"
    DICT_EDIT = "DICT_EDIT"
    DICT_DELETE = "DICT_DELETE"
    DICT_STATUS = "DICT_STATUS"

    CONFIG_CREATE = "CONFIG_CREATE"
    CONFIG_EDIT = "CONFIG_EDIT"
    CONFIG_DELETE = "CONFIG_DELETE"

    CONTENT_CREATE = "CONTENT_CREATE"
    CONTENT_EDIT = "CONTENT_EDIT"
    CONTENT_DELETE = "CONTENT_DELETE"
    CONTENT_PUBLISH = "CONTENT_PUBLISH"
    CONTENT_UNPUBLISH = "CONTENT_UNPUBLISH"
    CONTENT_REVIEW_INITIATE = "CONTENT_REVIEW_INITIATE"
    CONTENT_REVIEW_APPROVE = "CONTENT_REVIEW_APPROVE"
    CONTENT_REVIEW_REJECT = "CONTENT_REVIEW_REJECT"

    CHANNEL_UPDATE = "CHANNEL_UPDATE"

    PHYSICAL_CHANNEL_CREATE = "PHYSICAL_CHANNEL_CREATE"
    PHYSICAL_CHANNEL_DELETE = "PHYSICAL_CHANNEL_DELETE"

    CHANNEL_METADATA_CREATE = "CHANNEL_METADATA_CREATE"
    CHANNEL_METADATA_UPDATE = "CHANNEL_METADATA_UPDATE"
    CHANNEL_METADATA_DELETE = "CHANNEL_METADATA_DELETE"

    SCHEDULE_METADATA_CREATE = "SCHEDULE_METADATA_CREATE"
    SCHEDULE_METADATA_UPDATE = "SCHEDULE_METADATA_UPDATE"
    SCHEDULE_METADATA_DELETE = "SCHEDULE_METADATA_DELETE"

    CAST_ROLE_MAP_CREATE = "CAST_ROLE_MAP_CREATE"
    CAST_ROLE_MAP_UPDATE = "CAST_ROLE_MAP_UPDATE"
    CAST_ROLE_MAP_DELETE = "CAST_ROLE_MAP_DELETE"
    CAST_ROLE_MAP_LINK = "CAST_ROLE_MAP_LINK"
    CAST_ROLE_MAP_UNLINK = "CAST_ROLE_MAP_UNLINK"

    CONTENT_PACKAGE_LINK = "CONTENT_PACKAGE_LINK"
    CONTENT_PACKAGE_UNLINK = "CONTENT_PACKAGE_UNLINK"
    CONTENT_CATEGORY_LINK = "CONTENT_CATEGORY_LINK"
    CONTENT_CATEGORY_UNLINK = "CONTENT_CATEGORY_UNLINK"

    POSTER_UPLOAD = "POSTER_UPLOAD"
    POSTER_DELETE = "POSTER_DELETE"

    CHANNEL_FIELD_UPDATE = "CHANNEL_FIELD_UPDATE"
    CHANNEL_I18N_UPDATE = "CHANNEL_I18N_UPDATE"

    SCHEDULE_FIELD_UPDATE = "SCHEDULE_FIELD_UPDATE"
    SCHEDULE_I18N_UPDATE = "SCHEDULE_I18N_UPDATE"

    PROGRAM_METADATA_CREATE = "PROGRAM_METADATA_CREATE"
    PROGRAM_METADATA_UPDATE = "PROGRAM_METADATA_UPDATE"
    PROGRAM_METADATA_DELETE = "PROGRAM_METADATA_DELETE"

    SERIES_METADATA_CREATE = "SERIES_METADATA_CREATE"
    SERIES_METADATA_UPDATE = "SERIES_METADATA_UPDATE"
    SERIES_METADATA_DELETE = "SERIES_METADATA_DELETE"

    MOVIE_CREATE = "MOVIE_CREATE"
    MOVIE_UPDATE = "MOVIE_UPDATE"
    MOVIE_DELETE = "MOVIE_DELETE"

    VOD_FIELD_UPDATE = "VOD_FIELD_UPDATE"
    VOD_I18N_UPDATE = "VOD_I18N_UPDATE"

    EPISODE_INJECT = "EPISODE_INJECT"
    EPISODE_REMOVE = "EPISODE_REMOVE"

    PROVIDER_CREATE = "PROVIDER_CREATE"
    PROVIDER_EDIT = "PROVIDER_EDIT"
    PROVIDER_DELETE = "PROVIDER_DELETE"

    CONTRACT_CREATE = "CONTRACT_CREATE"
    CONTRACT_EDIT = "CONTRACT_EDIT"
    CONTRACT_DELETE = "CONTRACT_DELETE"

    LICENSE_CREATE = "LICENSE_CREATE"
    LICENSE_EDIT = "LICENSE_EDIT"
    LICENSE_DELETE = "LICENSE_DELETE"

    PACKAGE_CREATE = "PACKAGE_CREATE"
    PACKAGE_EDIT = "PACKAGE_EDIT"
    PACKAGE_DELETE = "PACKAGE_DELETE"
    PACKAGE_CONTENT_ADD = "PACKAGE_CONTENT_ADD"
    PACKAGE_CONTENT_REMOVE = "PACKAGE_CONTENT_REMOVE"

    PUBLISH_NOW = "PUBLISH_NOW"
    UNPUBLISH_NOW = "UNPUBLISH_NOW"
    PUBLISH_BATCH = "PUBLISH_BATCH"
    UNPUBLISH_BATCH = "UNPUBLISH_BATCH"
    PUBLISH_PLAN_CREATE = "PUBLISH_PLAN_CREATE"
    PUBLISH_PLAN_UPDATE = "PUBLISH_PLAN_UPDATE"
    PUBLISH_PLAN_CANCEL = "PUBLISH_PLAN_CANCEL"

    SCHEDULE_CREATE = "SCHEDULE_CREATE"
    SCHEDULE_DELETE = "SCHEDULE_DELETE"
    SCHEDULE_EXPORT = "SCHEDULE_EXPORT"
    SCHEDULE_IMPORT = "SCHEDULE_IMPORT"

    CAST_CREATE = "CAST_CREATE"
    CAST_EDIT = "CAST_EDIT"
    CAST_DELETE = "CAST_DELETE"

    CATEGORY_CREATE = "CATEGORY_CREATE"
    CATEGORY_EDIT = "CATEGORY_EDIT"
    CATEGORY_DELETE = "CATEGORY_DELETE"

    LICENSE_CONTENT_ADD = "LICENSE_CONTENT_ADD"
    LICENSE_CONTENT_REMOVE = "LICENSE_CONTENT_REMOVE"

    CONTRACT_ATTACHMENT_DELETE = "CONTRACT_ATTACHMENT_DELETE"
    CONTRACT_ATTACHMENT_UPLOAD = "CONTRACT_ATTACHMENT_UPLOAD"

    GENRE_CREATE = "GENRE_CREATE"
    GENRE_EDIT = "GENRE_EDIT"
    GENRE_DELETE = "GENRE_DELETE"

    TAG_CREATE = "TAG_CREATE"
    TAG_EDIT = "TAG_EDIT"
    TAG_DELETE = "TAG_DELETE"

    CUSTOM_TAG_CREATE = "CUSTOM_TAG_CREATE"
    CUSTOM_TAG_EDIT = "CUSTOM_TAG_EDIT"
    CUSTOM_TAG_DELETE = "CUSTOM_TAG_DELETE"

    CONTENT_TYPE_CREATE = "CONTENT_TYPE_CREATE"
    CONTENT_TYPE_EDIT = "CONTENT_TYPE_EDIT"
    CONTENT_TYPE_DELETE = "CONTENT_TYPE_DELETE"

    POSTER_SIZE_CREATE = "POSTER_SIZE_CREATE"
    POSTER_SIZE_EDIT = "POSTER_SIZE_EDIT"
    POSTER_SIZE_DELETE = "POSTER_SIZE_DELETE"

    CUSTOM_FIELD_CREATE = "CUSTOM_FIELD_CREATE"
    CUSTOM_FIELD_EDIT = "CUSTOM_FIELD_EDIT"
    CUSTOM_FIELD_DELETE = "CUSTOM_FIELD_DELETE"

    SENSITIVE_WORD_CREATE = "SENSITIVE_WORD_CREATE"
    SENSITIVE_WORD_EDIT = "SENSITIVE_WORD_EDIT"
    SENSITIVE_WORD_DELETE = "SENSITIVE_WORD_DELETE"
    SENSITIVE_WORD_STATUS = "SENSITIVE_WORD_STATUS"
    SENSITIVE_WORD_IMPORT = "SENSITIVE_WORD_IMPORT"
    SENSITIVE_WORD_EXPORT = "SENSITIVE_WORD_EXPORT"

    TASK_ASSIGN = "TASK_ASSIGN"
    TASK_COMPLETE = "TASK_COMPLETE"

    DATA_AUTH_AUTHORIZE = "DATA_AUTH_AUTHORIZE"
    DATA_AUTH_CLEAR = "DATA_AUTH_CLEAR"

    METADATA_SOURCE_CREATE = "METADATA_SOURCE_CREATE"
    METADATA_SOURCE_EDIT = "METADATA_SOURCE_EDIT"
    METADATA_SOURCE_DELETE = "METADATA_SOURCE_DELETE"
    METADATA_SOURCE_STATUS = "METADATA_SOURCE_STATUS"

    CRAWL_TASK_CREATE = "CRAWL_TASK_CREATE"
    CRAWL_TASK_RETRY = "CRAWL_TASK_RETRY"
    CRAWL_TASK_DELETE = "CRAWL_TASK_DELETE"
    CRAWL_TASK_CONFIRM = "CRAWL_TASK_CONFIRM"

    MENU_CREATE = "MENU_CREATE"
    MENU_EDIT = "MENU_EDIT"
    MENU_DELETE = "MENU_DELETE"
    MENU_ASSIGN = "MENU_ASSIGN"

    WORKFLOW_CREATE = "WORKFLOW_CREATE"
    WORKFLOW_EDIT = "WORKFLOW_EDIT"
    WORKFLOW_DELETE = "WORKFLOW_DELETE"
    WORKFLOW_PUBLISH = "WORKFLOW_PUBLISH"
    WORKFLOW_UNPUBLISH = "WORKFLOW_UNPUBLISH"
    WORKFLOW_NEW_VERSION = "WORKFLOW_NEW_VERSION"
    WORKFLOW_BATCH_PUBLISH = "WORKFLOW_BATCH_PUBLISH"

    METADATA_QUALITY_TRIGGER = "METADATA_QUALITY_TRIGGER"
    METADATA_QUALITY_DELETE = "METADATA_QUALITY_DELETE"
    METADATA_QUALITY_EXPORT = "METADATA_QUALITY_EXPORT"

    VALIDATION_RULE_CREATE = "VALIDATION_RULE_CREATE"
    VALIDATION_RULE_EDIT = "VALIDATION_RULE_EDIT"
    VALIDATION_RULE_DELETE = "VALIDATION_RULE_DELETE"
    VALIDATION_RULE_BATCH_DELETE = "VALIDATION_RULE_BATCH_DELETE"
    VALIDATION_RULE_IMPORT = "VALIDATION_RULE_IMPORT"

    SCHEDULED_TASK_TRIGGER = "SCHEDULED_TASK_TRIGGER"

    USAGE_LIMIT_UPDATE = "USAGE_LIMIT_UPDATE"

    OPERATION_LOG_EXPORT = "OPERATION_LOG_EXPORT"
    OPERATION_LOG_CLEAR = "OPERATION_LOG_CLEAR"

    PROVIDER_BATCH_DELETE = "PROVIDER_BATCH_DELETE"
    CONTRACT_BATCH_DELETE = "CONTRACT_BATCH_DELETE"
    LICENSE_BATCH_DELETE = "LICENSE_BATCH_DELETE"
    USER_BATCH_DELETE = "USER_BATCH_DELETE"
    USER_BATCH_STATUS = "USER_BATCH_STATUS"
    ROLE_BATCH_DELETE = "ROLE_BATCH_DELETE"
    ROLE_BATCH_STATUS = "ROLE_BATCH_STATUS"
    SENSITIVE_WORD_BATCH_DELETE = "SENSITIVE_WORD_BATCH_DELETE"
    SENSITIVE_WORD_BATCH_STATUS = "SENSITIVE_WORD_BATCH_STATUS"
    SENSITIVE_WORD_BATCH_EXPORT = "SENSITIVE_WORD_BATCH_EXPORT"
    METADATA_QUALITY_BATCH_DELETE = "METADATA_QUALITY_BATCH_DELETE"
    SCHEDULED_TASK_BATCH_TRIGGER = "SCHEDULED_TASK_BATCH_TRIGGER"
    CRAWL_TASK_BATCH_DELETE = "CRAWL_TASK_BATCH_DELETE"
    CUSTOM_FIELD_BATCH_DELETE = "CUSTOM_FIELD_BATCH_DELETE"
    TAG_BATCH_DELETE = "TAG_BATCH_DELETE"
    CATEGORY_BATCH_DELETE = "CATEGORY_BATCH_DELETE"
    CUSTOM_TAG_BATCH_DELETE = "CUSTOM_TAG_BATCH_DELETE"
    POSTER_SIZE_BATCH_DELETE = "POSTER_SIZE_BATCH_DELETE"
    CAST_BATCH_DELETE = "CAST_BATCH_DELETE"
    CONTENT_TYPE_BATCH_DELETE = "CONTENT_TYPE_BATCH_DELETE"
    METADATA_SOURCE_BATCH_ENABLE = "METADATA_SOURCE_BATCH_ENABLE"
    METADATA_SOURCE_BATCH_DISABLE = "METADATA_SOURCE_BATCH_DISABLE"
    METADATA_SOURCE_BATCH_DELETE = "METADATA_SOURCE_BATCH_DELETE"
    GENRE_BATCH_DELETE = "GENRE_BATCH_DELETE"
    CONTENT_BATCH_DELETE = "CONTENT_BATCH_DELETE"
    PACKAGE_BATCH_DELETE = "PACKAGE_BATCH_DELETE"
    TASK_BATCH_ASSIGN = "TASK_BATCH_ASSIGN"
    SCHEDULE_BATCH_EXPORT = "SCHEDULE_BATCH_EXPORT"

# ── 写入日志 ────────────────────────────────────────────────

async def write_log(
    db: AsyncSession,
    *,
    user_id: int | None,
    user_name: str | None,
    operation_type: str,
    operation_object: str | None = None,
    operation_content: str | None = None,
    content_id: int | None = None,
    entity_type: str | None = None,
    entity_id: int | None = None,
    previous_value: str | None = None,
    updated_value: str | None = None,
    updated_value_json: str | None = None,
    ip_address: str | None = None,
    result: str = "success",
    error_message: str | None = None,
) -> None:
    """写入操作日志，内部捕获所有异常，不影响主业务流程。

    previous_value / updated_value 传入已序列化的 JSON 字符串，
    供详情页 Processed History 的 Previous / Updated value 两列展示。
    entity_type / entity_id 用于详情页历史精确查询。
    """
    try:
        log = OperationLog(
            user_id=user_id,
            user_name=user_name,
            operation_type=operation_type,
            operation_object=operation_object,
            operation_content=operation_content,
            previous_value=previous_value,
            updated_value=updated_value,
            updated_value_json=updated_value_json,
            content_id=content_id,
            entity_type=entity_type,
            entity_id=entity_id,
            ip_address=ip_address,
            result=result,
            error_message=error_message,
        )
        db.add(log)
        await db.flush()
    except Exception as exc:
        logger.warning("操作日志写入失败: %s", exc)

# ── 通用实体历史查询 ──────────────────────────────────────────

async def list_entity_history(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    limit: int = 100,
) -> list:
    from app.internal.cms_biz_system.schemas.operation_log import ProcessedHistoryItem
    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.entity_type == entity_type,
                OperationLog.entity_id == entity_id,
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        ProcessedHistoryItem(
            id=log.id,
            processed_at=log.operation_time,
            processed_by=log.user_name,
            processed_type=log.operation_type,
            entity_type=log.entity_type,
            details=log.operation_content,
            previous_value=log.previous_value,
            updated_value=log.updated_value,
            updated_value_json=log.updated_value_json,
        )
        for log in logs
    ]


async def list_content_history(
    db: AsyncSession,
    content_id: int,
    limit: int = 100,
) -> list:
    from app.internal.cms_biz_system.schemas.operation_log import ProcessedHistoryItem
    from app.internal.cms_biz_package.models.package import Content
    child_ids = (
        await db.execute(
            select(Content.id).where(Content.parent_id == content_id, Content.is_deleted.is_(False))
        )
    ).scalars().all()
    all_content_ids = [content_id] + list(child_ids)
    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.content_id.in_(all_content_ids),
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        ProcessedHistoryItem(
            id=log.id,
            processed_at=log.operation_time,
            processed_by=log.user_name,
            processed_type=log.operation_type,
            entity_type=log.entity_type,
            details=log.operation_content,
            previous_value=log.previous_value,
            updated_value=log.updated_value,
            updated_value_json=log.updated_value_json,
        )
        for log in logs
    ]

# ── 查询 ─────────────────────────────────────────────────────

async def list_logs(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    user_name: str | None = None,
    operation_type: str | None = None,
    operation_object: str | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    result: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[OperationLogItem]:
    query = select(OperationLog)

    if user_name:
        query = query.where(OperationLog.user_name.ilike(f"%{user_name}%"))
    if operation_type:
        query = query.where(OperationLog.operation_type == operation_type)
    if operation_object:
        query = query.where(OperationLog.operation_object.ilike(f"%{operation_object}%"))
    if time_start:
        query = query.where(OperationLog.operation_time >= time_start)
    if time_end:
        query = query.where(OperationLog.operation_time <= time_end)
    if result:
        query = query.where(OperationLog.result == result)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(OperationLog, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func)
        else:
            query = query.order_by(OperationLog.operation_time.desc())
    else:
        query = query.order_by(OperationLog.operation_time.desc())

    logs = (
        await db.execute(
            query.offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[OperationLogItem.model_validate(log) for log in logs],
    )

async def get_log(db: AsyncSession, log_id: int) -> OperationLog:
    from fastapi import HTTPException, status as http_status
    log = (
        await db.execute(select(OperationLog).where(OperationLog.id == log_id))
    ).scalar_one_or_none()
    if not log:
        raise NotFoundException(ErrorCode.NOT_FOUND, get_msg("OPERATION_LOG_NOT_FOUND"))
    return log

# ── 清空 ─────────────────────────────────────────────────────

async def clear_logs(db: AsyncSession, start: datetime, end: datetime) -> int:
    result = await db.execute(
        delete(OperationLog).where(
            OperationLog.operation_time >= start,
            OperationLog.operation_time <= end,
        )
    )
    await db.commit()
    return result.rowcount

# ── Excel 导出 ────────────────────────────────────────────────

async def export_logs_excel(
    db: AsyncSession,
    user_name: str | None = None,
    operation_type: str | None = None,
    operation_object: str | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    result: str | None = None,
    lang: str = "cn",
) -> bytes:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from app.common.core.i18n import get_message

    query = select(OperationLog)
    if user_name:
        query = query.where(OperationLog.user_name.ilike(f"%{user_name}%"))
    if operation_type:
        query = query.where(OperationLog.operation_type == operation_type)
    if operation_object:
        query = query.where(OperationLog.operation_object.ilike(f"%{operation_object}%"))
    if time_start:
        query = query.where(OperationLog.operation_time >= time_start)
    if time_end:
        query = query.where(OperationLog.operation_time <= time_end)
    if result:
        query = query.where(OperationLog.result == result)

    logs = (
        await db.execute(query.order_by(OperationLog.operation_time.desc()))
    ).scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = get_message("EXPORT_SHEET_TITLE", lang=lang)

    headers = [
        get_message("EXPORT_COL_USER", lang=lang),
        get_message("EXPORT_COL_TYPE", lang=lang),
        get_message("EXPORT_COL_OBJECT", lang=lang),
        get_message("EXPORT_COL_CONTENT", lang=lang),
        get_message("EXPORT_COL_TIME", lang=lang),
        get_message("EXPORT_COL_IP", lang=lang),
        get_message("EXPORT_COL_RESULT", lang=lang),
        get_message("EXPORT_COL_ERROR", lang=lang),
    ]
    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    col_widths = [16, 18, 24, 40, 22, 16, 12, 30]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    for row_idx, log in enumerate(logs, 2):
        op_time = log.operation_time.strftime("%Y-%m-%d %H:%M:%S") if log.operation_time else ""
        op_type = get_message(log.operation_type, lang=lang) if log.operation_type else ""
        ws.cell(row=row_idx, column=1, value=log.user_name or "")
        ws.cell(row=row_idx, column=2, value=op_type)
        ws.cell(row=row_idx, column=3, value=log.operation_object or "")
        ws.cell(row=row_idx, column=4, value=log.operation_content or "")
        ws.cell(row=row_idx, column=5, value=op_time)
        ws.cell(row=row_idx, column=6, value=log.ip_address or "")
        ws.cell(row=row_idx, column=7, value=get_message("EXPORT_RESULT_SUCCESS" if log.result == "success" else "EXPORT_RESULT_FAILED", lang=lang))
        ws.cell(row=row_idx, column=8, value=log.error_message or "")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
