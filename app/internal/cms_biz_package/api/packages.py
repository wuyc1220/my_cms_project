"""
服务包（Package）API 路由层。

路由前缀：/packages

接口列表：
    GET    /                      查询服务包列表（分页 + 过滤）
    POST   /                      新建服务包
    DELETE /batch                 批量软删除服务包
    GET    /{package_id}          查询单个服务包详情
    PUT    /{package_id}          编辑服务包
    DELETE /{package_id}          软删除服务包
    GET    /{package_id}/contents 查询已关联内容列表
    POST   /{package_id}/contents 向服务包添加内容（支持批量）
    DELETE /{package_id}/contents/{content_id}  从服务包移除内容
    GET    /{package_id}/available-contents      查询可添加内容列表（弹框数据源）
    GET    /{package_id}/field-values            查询自定义字段值
    PUT    /{package_id}/field-values            保存自定义字段值
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_package.models.package import Package
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_package.schemas.package import (
    ContentSimpleItem,
    PackageContentAddRequest,
    PackageCreate,
    PackageListItem,
    PackageUpdate,
)
from app.internal.cms_biz_metada.schemas.basic import EntityFieldValueItem, EntityFieldValuesPayload
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_package.services import package_service
from app.internal.cms_biz_metada.services.entity_data_service import get_field_values, save_field_values, get_i18n_values, save_i18n_values
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_metada.schemas.basic import EntityI18nItem, EntityI18nPayload

router = APIRouter(prefix="/packages")

ENTITY_TYPE = "package"


@router.get("/", response_model=PaginatedResponse[PackageListItem])
async def get_package_list(
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    package_type: str | None = None,
    platforms: list[str] | None = Query(default=None),
    ingest_statuses: list[str] | None = Query(default=None),
    description: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await package_service.list_packages(
        db, page, page_size, name, package_type, platforms, ingest_statuses, description, sort_by, sort_order
    )


@router.post("/", response_model=PackageListItem)
async def create_package_api(
    body: PackageCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pkg = await package_service.create_package(db, body)
    new_data = orm_to_dict(pkg)
    prev_val, new_val, raw_val = await prepare_log_values(db, "package", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_CREATE,
        operation_object=f"服务包 {body.name}",
        operation_content=f"Created package: name={body.name}, type={body.package_type}",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="package",
        entity_id=pkg.id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return package_service._to_list_item(pkg)


@router.delete("/batch")
async def batch_delete_packages_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Package.name).where(Package.id.in_(body.ids)))).scalars().all()
    pkg_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await package_service.batch_delete_packages(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_BATCH_DELETE,
        operation_object=f"服务包 {pkg_names}",
        operation_content=f"批量删除服务包: {pkg_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="package",
        entity_id=body.ids[0] if body.ids else None,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.get("/{package_id}", response_model=PackageListItem)
async def get_package_detail(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    pkg = await package_service.get_package(db, package_id)
    return package_service._to_list_item(pkg)


@router.put("/{package_id}", response_model=PackageListItem)
async def update_package_api(
    package_id: int,
    body: PackageUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await package_service.get_package(db, package_id)
    old_data = orm_to_dict(old)
    pkg = await package_service.update_package(db, package_id, body)
    new_data = orm_to_dict(pkg)
    prev_val, new_val, raw_val = await prepare_log_values(db, "package", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_EDIT,
        operation_object=f"服务包 {old.name}",
        operation_content=f"Updated package: ID={package_id}, name={pkg.name}",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="package",
        entity_id=package_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return package_service._to_list_item(pkg)


@router.delete("/{package_id}")
async def delete_package_api(
    package_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pkg = await package_service.get_package(db, package_id)
    pkg_name = pkg.name
    old_data = orm_to_dict(pkg)
    prev_val, new_val, raw_val = await prepare_log_values(db, "package", old_data, None)
    await package_service.delete_package(db, package_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_DELETE,
        operation_object=f"服务包 {pkg_name}",
        operation_content=f"Deleted package: ID={package_id}, name={pkg_name}",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="package",
        entity_id=package_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


# ─── Package↔Content 关联 ─────────────────────────────────────────────

@router.get("/{package_id}/contents", response_model=PaginatedResponse[ContentSimpleItem])
async def get_package_contents(
    package_id: int,
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await package_service.list_package_contents(db, package_id, page, page_size)


@router.post("/{package_id}/contents", response_model=list[ContentSimpleItem])
async def add_contents_to_package_api(
    package_id: int,
    body: PackageContentAddRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await package_service.add_contents_to_package(db, package_id, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_CONTENT_ADD,
        operation_object=f"服务包 ID={package_id}",
        operation_content=f"Linked content to package: package ID={package_id}, content IDs={body.content_ids}",
        entity_type="package",
        entity_id=package_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/{package_id}/contents/{content_id}")
async def remove_content_from_package_api(
    package_id: int,
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    await package_service.remove_content_from_package(db, package_id, content_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_CONTENT_REMOVE,
        operation_object=f"服务包 ID={package_id}",
        operation_content=f"Removed content from package: package ID={package_id}, content ID={content_id}",
        entity_type="package",
        entity_id=package_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/{package_id}/available-contents", response_model=PaginatedResponse[ContentSimpleItem])
async def get_available_contents(
    package_id: int,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = Query(default=None),
    genre_ids: list[int] | None = Query(default=None),
    custom_tag_ids: list[int] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await package_service.list_available_contents(
        db, package_id, page, page_size, title, content_types, genre_ids, custom_tag_ids
    )


# ─── 自定义字段值 ──────────────────────────────────────────────────────

@router.get("/{package_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_package_field_values(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await package_service.get_package(db, package_id)
    return await get_field_values(db, ENTITY_TYPE, package_id)


@router.put("/{package_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_package_field_values(
    package_id: int,
    body: EntityFieldValuesPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await package_service.get_package(db, package_id)
    return await save_field_values(db, ENTITY_TYPE, package_id, body)


@router.get("/{package_id}/i18n", response_model=list[EntityI18nItem])
async def get_package_i18n(
    package_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await package_service.get_package(db, package_id)
    return await get_i18n_values(db, ENTITY_TYPE, package_id)


@router.put("/{package_id}/i18n", response_model=list[EntityI18nItem])
async def save_package_i18n(
    package_id: int,
    body: EntityI18nPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await package_service.get_package(db, package_id)
    return await save_i18n_values(db, ENTITY_TYPE, package_id, body)
