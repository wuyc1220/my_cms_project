from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user
from app.common.dependencies import get_db
from app.common.core.i18n import get_accept_language
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.operation_log import ClearLogsRequest, OperationLogItem, ProcessedHistoryItem
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.services import operation_log_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log, translate_log_value

router = APIRouter(prefix="/operation-logs")


@router.get("/", response_model=PaginatedResponse[OperationLogItem])
async def get_operation_logs(
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
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await operation_log_service.list_logs(
        db,
        page=page,
        page_size=page_size,
        user_name=user_name,
        operation_type=operation_type,
        operation_object=operation_object,
        time_start=time_start,
        time_end=time_end,
        result=result,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get("/history", response_model=list[ProcessedHistoryItem])
async def get_entity_history(
    entity_type: str,
    entity_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await operation_log_service.list_entity_history(db, entity_type, entity_id, limit)


@router.get("/content-history", response_model=list[ProcessedHistoryItem])
async def get_content_history(
    content_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await operation_log_service.list_content_history(db, content_id, limit)


@router.get("/export")
async def export_operation_logs(
    request: Request,
    user_name: str | None = None,
    operation_type: str | None = None,
    operation_object: str | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    result: str | None = None,
    ids: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    lang = get_accept_language(request.headers.get("accept-language"))
    id_list: list[int] | None = None
    if ids:
        id_list = [int(i) for i in ids.split(",") if i.strip().isdigit()]
    data = await operation_log_service.export_logs_excel(
        db,
        user_name=user_name,
        operation_type=operation_type,
        operation_object=operation_object,
        time_start=time_start,
        time_end=time_end,
        result=result,
        ids=id_list,
        lang=lang,
    )
    import json
    raw_val = json.dumps({"log_ids": id_list}, ensure_ascii=False) if id_list else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.OPERATION_LOG_EXPORT,
        operation_object_code="OBJ_OPERATION_LOG",
        operation_content_code="LOG_OPERATION_LOG_EXPORT",
        ip_address=_get_ip(request),
        result="success",
        entity_type="operation_log",
        previous_value=None,
        updated_value=f"导出操作日志 {len(id_list) if id_list else '全部'} 条",
        updated_value_json=raw_val,
    )
    await db.commit()
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=operation_logs.xlsx"},
    )


@router.get("/{log_id}", response_model=OperationLogItem)
async def get_operation_log(
    log_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    log = await operation_log_service.get_log(db, log_id)
    result = OperationLogItem.model_validate(log)
    result.operation_object = translate_log_value(log.operation_object)
    result.operation_content = translate_log_value(log.operation_content)
    return result


@router.delete("/clear")
async def clear_operation_logs(
    body: ClearLogsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = await operation_log_service.clear_logs(db, body.start, body.end)
    import json
    raw_val = json.dumps({
        "start": str(body.start) if body.start else None,
        "end": str(body.end) if body.end else None,
        "deleted": deleted
    }, ensure_ascii=False)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.OPERATION_LOG_CLEAR,
        operation_object_code="OBJ_OPERATION_LOG",
        operation_content_code="LOG_OPERATION_LOG_CLEAR", operation_content_params={"deleted": deleted},
        ip_address=_get_ip(request),
        result="success",
        entity_type="operation_log",
        previous_value=None,
        updated_value=f"清空操作日志 {deleted} 条",
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}
