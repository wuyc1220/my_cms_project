"""运维监控 - 元数据质量检查 API。"""

from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db, require_permission
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_system.models.metadata_quality import MetadataQualityCheck
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.metadata_quality import (
    DeleteMetadataChecksResponse,
    MetadataQualityCheckDetail,
    MetadataQualityCheckOut,
    MetadataQualityIssueOut,
    TriggerMetadataCheckResponse,
)
from app.internal.cms_biz_system.services.metadata_quality_service import (
    delete_metadata_quality_checks,
    export_metadata_quality_report,
    get_metadata_quality_check,
    list_metadata_quality_checks,
    list_metadata_quality_issues,
    trigger_metadata_quality_check,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log

router = APIRouter(prefix="/metadata-quality-checks")


@router.get("/", response_model=PaginatedResponse[MetadataQualityCheckOut])
async def api_list_checks(
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    status: list[str] | None = Query(None),
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_metadata_quality_checks(
        db, page, page_size, time_start, time_end, status, sort_by, sort_order,
    )


@router.get("/{check_id}", response_model=MetadataQualityCheckDetail)
async def api_get_check(
    check_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_metadata_quality_check(db, check_id)


@router.get("/{check_id}/issues", response_model=PaginatedResponse[MetadataQualityIssueOut])
async def api_list_issues(
    check_id: int,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=100),
    sort_by: str | None = Query(None),
    sort_order: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_metadata_quality_issues(db, check_id, page, page_size, sort_by, sort_order)


@router.post("/trigger", response_model=TriggerMetadataCheckResponse)
async def api_trigger_check(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: User = Depends(require_permission("menu.ops.monitor.operate")),
):
    new_id = await trigger_metadata_quality_check(db, operator=current_user.username)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_QUALITY_TRIGGER,
        operation_object_code="OBJ_QUALITY_CHECK", operation_object_params={"name": new_id},
        operation_content_code="LOG_METADATA_QUALITY_TRIGGER", operation_content_params={"id": new_id},
        ip_address=_get_ip(request),
        result="success",
        entity_type="metadata_quality_check",
        entity_id=new_id,
    )
    await db.commit()
    return TriggerMetadataCheckResponse(id=new_id)


@router.delete("/batch", response_model=DeleteMetadataChecksResponse)
async def api_batch_delete_checks(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    _: User = Depends(require_permission("menu.ops.monitor.operate")),
):
    checks = (await db.execute(select(MetadataQualityCheck).where(MetadataQualityCheck.id.in_(body.ids), MetadataQualityCheck.is_deleted.is_(False)))).scalars().all()
    check_ids = ", ".join([str(c.id) for c in checks]) if checks else str(body.ids)
    prev_data = [orm_to_dict(c) for c in checks]
    prev_val, _, raw_val = await prepare_log_values(db, "metadata_quality_check", prev_data, None)
    deleted = await delete_metadata_quality_checks(db, body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_QUALITY_BATCH_DELETE,
        operation_object_code="OBJ_QUALITY_CHECK", operation_object_params={"name": check_ids},
        operation_content_code="LOG_METADATA_QUALITY_BATCH_DELETE", operation_content_params={"names": check_ids},
        ip_address=_get_ip(request),
        result="success",
        entity_type="metadata_quality_check",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    return DeleteMetadataChecksResponse(deleted=deleted)


@router.get("/{check_id}/report")
async def api_download_report(
    check_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data, filename = await export_metadata_quality_report(db, check_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.METADATA_QUALITY_EXPORT,
        operation_object_code="OBJ_QUALITY_CHECK", operation_object_params={"name": check_id},
        operation_content_code="LOG_METADATA_QUALITY_EXPORT", operation_content_params={"id": check_id},
        ip_address=_get_ip(request),
        result="success",
        entity_type="metadata_quality_check",
        entity_id=check_id,
    )
    await db.commit()
    encoded = quote(filename)
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}; filename*=UTF-8''{encoded}"},
    )
