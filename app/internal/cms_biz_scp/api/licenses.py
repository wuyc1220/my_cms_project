"""
许可证（License）API 路由层。

路由前缀：/licenses

接口列表：
    GET    /                               查询许可证列表（分页 + 过滤）
    POST   /                               新建许可证
    DELETE /batch                          批量软删除许可证
    GET    /without-license-content-count  统计无许可证内容数量
    GET    /{license_id}                   查询单个许可证详情
    PUT    /{license_id}                   编辑许可证
    DELETE /{license_id}                   软删除许可证
    GET    /{license_id}/contents          查询许可证已关联内容列表
    POST   /{license_id}/contents          向许可证添加内容（支持批量）
    DELETE /{license_id}/contents/{id}     从许可证移除内容
    GET    /{license_id}/available-contents 查询可添加内容列表（弹框数据源）
    GET    /{license_id}/history            查询许可证操作历史（Processed History）
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_scp.models.trade import License, LicenseContent
from app.internal.cms_biz_scp.schemas.trade import (
    ContentAddToLicenseRequest,
    ContentForTradeItem,
    LicenseCreate,
    LicenseHistoryItem,
    LicenseListItem,
    LicenseUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_scp.services import license_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log

router = APIRouter(prefix="/licenses")


@router.get("/", response_model=PaginatedResponse[LicenseListItem])
async def get_license_list(
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    service_types: list[str] | None = Query(default=None),
    platforms: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    start_date_from: str | None = None,
    start_date_to: str | None = None,
    end_date_from: str | None = None,
    end_date_to: str | None = None,
    contract_id: int | None = None,
    provider_id: int | None = None,
    without_content: bool = False,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await license_service.list_licenses(
        db, page, page_size, name, service_types, platforms, statuses,
        start_date_from, start_date_to,
        end_date_from, end_date_to,
        contract_id, provider_id,
        without_content, sort_by, sort_order,
    )


@router.post("/", response_model=LicenseListItem)
async def create_license_api(
    body: LicenseCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await license_service.create_license(db, body)
    new_data = orm_to_dict(result)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "license", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.LICENSE_CREATE,
        operation_object_code="OBJ_LICENSE", operation_object_params={"name": body.name},
        operation_content_code="LOG_LICENSE_CREATE", operation_content_params={"name": body.name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="license",
        entity_id=result.id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/batch")
async def batch_delete_licenses_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    licenses = (await db.execute(select(License).where(License.id.in_(body.ids), License.is_deleted.is_(False)))).scalars().all()
    license_names = ", ".join([l.name for l in licenses]) if licenses else str(body.ids)
    prev_data = [orm_to_dict(l) for l in licenses]
    prev_val, _, raw_val = await prepare_log_values(db, "license", prev_data, None)
    deleted = await license_service.batch_delete_licenses(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.LICENSE_BATCH_DELETE,
        operation_object_code="OBJ_LICENSE", operation_object_params={"name": license_names},
        operation_content_code="LOG_LICENSE_BATCH_DELETE", operation_content_params={"names": license_names},
        entity_type="license",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.get("/without-license-content-count")
async def get_without_license_content_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = await license_service.get_without_license_content_count(db, current_user)
    return {"count": count}


@router.get("/{license_id}", response_model=LicenseListItem)
async def get_license_detail(
    license_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await license_service.get_license(db, license_id)


@router.put("/{license_id}", response_model=LicenseListItem)
async def update_license_api(
    license_id: int,
    body: LicenseUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await license_service.get_license(db, license_id)
    result = await license_service.update_license(db, license_id, body)
    old_data = orm_to_dict(old)
    new_data = orm_to_dict(result)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "license", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.LICENSE_EDIT,
        operation_object_code="OBJ_LICENSE", operation_object_params={"name": old.name},
        operation_content_code="LOG_LICENSE_EDIT", operation_content_params={"name": old.name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="license",
        entity_id=license_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/{license_id}")
async def delete_license_api(
    license_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    license_ = await license_service.get_license(db, license_id)
    license_name = license_.name
    old_data = orm_to_dict(license_)
    await license_service.delete_license(db, license_id)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "license", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.LICENSE_DELETE,
        operation_object_code="OBJ_LICENSE", operation_object_params={"name": license_name},
        operation_content_code="LOG_LICENSE_DELETE", operation_content_params={"name": license_name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="license",
        entity_id=license_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


# ─── License↔Content 关联 ─────────────────────────────────────────────

@router.get("/{license_id}/contents", response_model=list[ContentForTradeItem])
async def get_license_contents(
    license_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await license_service.list_license_contents(db, license_id, current_user)


@router.post("/{license_id}/contents", response_model=list[ContentForTradeItem])
async def add_contents_to_license_api(
    license_id: int,
    body: ContentAddToLicenseRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # 查询调用前已关联的内容，仅对本次实际新增的内容写日志
    # （service 幂等跳过已关联内容，API 日志与其保持一致，重复提交不产生冗余日志）
    existing_ids = set(
        (
            await db.execute(
                select(LicenseContent.content_id).where(
                    LicenseContent.license_id == license_id,
                    LicenseContent.content_id.in_(body.content_ids),
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
    )
    result = await license_service.add_contents_to_license(db, license_id, body, current_user)
    import json
    license_obj = await license_service.get_license(db, license_id)
    license_name = license_obj.name if license_obj else str(license_id)
    raw_val = json.dumps({
        "license_id": license_id,
        "license_name": license_name,
        "content_ids": body.content_ids
    }, ensure_ascii=False)
    upd_val = json.dumps({"license_name": license_name}, ensure_ascii=False)
    # 逐个新增内容写日志（content_id 各自归属），保证每个被直接关联的内容详情页 Activity Log 可见
    for cid in body.content_ids:
        if cid in existing_ids:
            continue
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.LICENSE_CONTENT_ADD,
            operation_object_code="log.license.link",
            operation_content_code="log.license.link",
            content_id=cid,
            entity_type="license",
            entity_id=license_id,
            previous_value=None,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{license_id}/contents/{content_id}")
async def remove_content_from_license_api(
    license_id: int,
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json
    license_obj = await license_service.get_license(db, license_id)
    license_name = license_obj.name if license_obj else str(license_id)
    raw_val = json.dumps({
        "license_id": license_id,
        "license_name": license_name,
        "content_id": content_id
    }, ensure_ascii=False)
    prev_val = json.dumps({"license_name": license_name}, ensure_ascii=False)
    await license_service.remove_content_from_license(
        db, license_id, content_id,
        processed_by=current_user.username, current_user=current_user,
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.LICENSE_CONTENT_REMOVE,
        operation_object_code="log.license.unlink",
        operation_content_code="log.license.unlink",
        content_id=content_id,
        entity_type="license",
        entity_id=license_id,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/available-contents/unlicensed", response_model=PaginatedResponse[ContentForTradeItem])
async def get_unlicensed_contents(
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = Query(default=None),
    ingest_statuses: list[str] | None = Query(default=None),
    genre_ids: list[int] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await license_service.list_unlicensed_contents(
        db, page, page_size, title, content_types, ingest_statuses, genre_ids, current_user
    )


@router.get("/{license_id}/available-contents", response_model=PaginatedResponse[ContentForTradeItem])
async def get_available_contents(
    license_id: int,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = Query(default=None),
    ingest_statuses: list[str] | None = Query(default=None),
    genre_ids: list[int] | None = Query(default=None),
    without_license: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await license_service.list_available_contents(
        db, license_id, page, page_size, title, content_types, ingest_statuses, genre_ids, without_license, current_user
    )


@router.get("/{license_id}/history", response_model=list[LicenseHistoryItem])
async def get_license_history(
    license_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await license_service.list_license_history(db, license_id, limit)
