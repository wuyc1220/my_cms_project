"""
服务包（Package）API 路由层。

路由前缀：/packages

接口列表：
    GET    /                      查询服务包列表（分页 + 过滤）
    POST   /                      新建服务包
    DELETE /batch                 批量软删除服务包
    POST   /export                导出服务包为 Excel
    POST   /import                导入服务包内容关联
    GET    /download-template     下载导入模板
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

import io

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_package.models.package import Content, ContentPackage, Package
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
from app.common.core.i18n import get_msg
from app.soap.c2 import PackageSyncBuilder
from pydantic import BaseModel

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
        operation_object_code="OBJ_PACKAGE", operation_object_params={"name": body.name},
        operation_content_code="LOG_PACKAGE_CREATE", operation_content_params={"name": body.name},
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
    packages = (await db.execute(select(Package).where(Package.id.in_(body.ids)))).scalars().all()
    pkg_names = ", ".join([p.name for p in packages]) if packages else str(body.ids)
    prev_data = [orm_to_dict(p) for p in packages]
    prev_val, _, raw_val = await prepare_log_values(db, "package", prev_data, None)
    deleted = await package_service.batch_delete_packages(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_BATCH_DELETE,
        operation_object_code="OBJ_PACKAGE", operation_object_params={"name": pkg_names},
        operation_content_code="LOG_PACKAGE_BATCH_DELETE", operation_content_params={"names": pkg_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="package",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


# ─── Package Export/Import（导出/导入）────────────────────────────────

@router.post("/export")
async def export_packages_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导出服务包为 Excel 文件。"""
    from app.common.core.exceptions import BusinessException, ErrorCode
    from app.common.core.i18n import get_msg
    from app.internal.cms_biz_system.services.operation_log_service import OperationType

    if not body.ids:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("PACKAGE_IDS_REQUIRED"))

    excel_data = await package_service.export_packages_excel(db, body.ids)

    import json
    raw_val = json.dumps({"package_ids": body.ids}, ensure_ascii=False)

    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_EXPORT,
        operation_object_code="OBJ_PACKAGE",
        operation_content_code="LOG_PACKAGE_EXPORT", operation_content_params={"count": len(body.ids)},
        ip_address=_get_ip(request),
        result="success",
        entity_type="package",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=None,
        updated_value=f"导出 {len(body.ids)} 个服务包",
        updated_value_json=raw_val,
    )
    await db.commit()

    return StreamingResponse(
        io.BytesIO(excel_data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=packages.xlsx"},
    )


@router.post("/import")
async def import_package_contents_api(
    file: UploadFile,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导入服务包内容关联关系。"""
    from app.common.core.exceptions import BusinessException, ErrorCode
    from app.common.core.i18n import get_msg
    from app.internal.cms_biz_system.services.operation_log_service import OperationType

    if not file.filename or not file.filename.endswith((".xlsx", ".xls")):
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("INVALID_FILE_TYPE"))

    file_content = await file.read()
    result, added, removed = await package_service.import_package_contents_excel(
        db, file_content, current_user.username,
    )

    import json
    raw_val = json.dumps({
        "filename": file.filename,
        "total": result.total,
        "created": result.created,
        "deleted": result.deleted,
        "skipped": result.skipped,
        "errors": [str(e) for e in result.errors] if result.errors else []
    }, ensure_ascii=False)

    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PACKAGE_IMPORT,
        operation_object_code="OBJ_PACKAGE",
        operation_content_code="LOG_PACKAGE_IMPORT", operation_content_params={"result": f"total={result.total}, created={result.created}, deleted={result.deleted}, skipped={result.skipped}"},
        ip_address=_get_ip(request),
        result="success" if not result.errors else "partial",
        entity_type="package",
        previous_value=None,
        updated_value=f"导入: 创建={result.created}, 删除={result.deleted}, 跳过={result.skipped}",
        updated_value_json=raw_val,
    )

    # 按内容维度补写操作日志（带 content_id，使内容详情页 Activity Log 可见）。
    # 格式与内容详情页勾选/取消勾选服务包（live.py link/unlink_content_packages_api）
    # 完全一致：CONTENT_PACKAGE_LINK/UNLINK + package_names，
    # 前端据此渲染为"关联服务包: 包名"/"取消关联服务包: 包名"
    for item in added:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CONTENT_PACKAGE_LINK,
            operation_object_code="log.package.link",
            operation_content_code="log.package.link",
            content_id=item["content_id"],
            entity_type="content",
            entity_id=item["content_id"],
            updated_value=json.dumps(
                {"package_names": item["package_name"] or ''},
                ensure_ascii=False,
            ),
            # 原始入参快照：与内容详情页关联口径一致
            updated_value_json=json.dumps(
                {"content_id": item["content_id"], "package_id": item["package_id"]},
                ensure_ascii=False,
            ),
            ip_address=_get_ip(request),
            result="success",
        )
    for item in removed:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CONTENT_PACKAGE_UNLINK,
            operation_object_code="log.package.unlink",
            operation_content_code="log.package.unlink",
            content_id=item["content_id"],
            entity_type="content",
            entity_id=item["content_id"],
            previous_value=json.dumps(
                {"package_names": item["package_name"] or ''},
                ensure_ascii=False,
            ),
            updated_value=None,
            updated_value_json=json.dumps(
                {"content_id": item["content_id"], "package_id": item["package_id"]},
                ensure_ascii=False,
            ),
            ip_address=_get_ip(request),
            result="success",
        )

    await db.commit()

    return result


@router.get("/download-template")
async def download_import_template(
    _: User = Depends(get_current_user),
):
    """下载导入模板。"""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from app.internal.cms_biz_system.services.operation_log_service import OperationType

    wb = Workbook()
    ws = wb.active
    ws.title = "PackageContents"

    headers = ["Package Name", "Content Name", "Operation Mode"]
    filename = "Package_Import_Template.xlsx"
    example_data = [
        ["示例套餐", "示例内容1", "Add"],
        ["示例套餐", "示例内容2", "DEL"],
    ]

    # 表头样式
    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    # 写入表头
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 写入示例数据
    for row_idx, row_data in enumerate(example_data, 2):
        for col_idx, value in enumerate(row_data, 1):
            ws.cell(row=row_idx, column=col_idx, value=value)

    # 设置列宽
    col_widths = [25, 30, 20]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    # 保存到内存
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ─── Package Publish Check ────────────────────────────────────────────

class PackagePublishCheckResponse(BaseModel):
    """Package 发布状态校验响应"""
    can_publish: bool
    total_count: int
    published_count: int
    unpublished_count: int
    unpublished_packages: list[dict] = []
    message: str = ""


@router.get("/publish-check", response_model=PackagePublishCheckResponse)
async def check_packages_publish_status(
    content_id: int = Query(..., description="Content ID (Schedule)"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    校验 Schedule 关联的 Package 是否已发布。

    用于节目单发布前的校验：查询 ScheduleMetadata.package_ids，
    检查所有关联 Package 的 ingest_status 是否为 'success'。
    """
    from app.internal.cms_biz_orchestration.models.content_metadata import ScheduleMetadata

    # 查询 Schedule 的元数据
    meta = (await db.execute(
        select(ScheduleMetadata).where(
            ScheduleMetadata.content_id == content_id,
            ScheduleMetadata.is_deleted.is_(False),
        )
    )).scalar_one_or_none()

    if not meta or not meta.package_ids:
        return PackagePublishCheckResponse(
            can_publish=True,
            total_count=0,
            published_count=0,
            unpublished_count=0,
            unpublished_packages=[],
            message="",
        )

    # 查询关联的 Package
    packages = (await db.execute(
        select(Package).where(
            Package.id.in_(meta.package_ids),
            Package.is_deleted.is_(False),
        )
    )).scalars().all()

    total = len(packages)
    published = [p for p in packages if p.ingest_status == 'success']
    unpublished = [p for p in packages if p.ingest_status != 'success']

    return PackagePublishCheckResponse(
        can_publish=len(unpublished) == 0,
        total_count=total,
        published_count=len(published),
        unpublished_count=len(unpublished),
        unpublished_packages=[
            {"id": p.id, "name": p.name, "ingest_status": p.ingest_status}
            for p in unpublished
        ],
        message=get_msg("PACKAGE_UNPUBLISHED_WARNING", count=len(unpublished)) if unpublished else "",
    )


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
        operation_object_code="OBJ_PACKAGE", operation_object_params={"name": old.name},
        operation_content_code="LOG_PACKAGE_EDIT", operation_content_params={"name": pkg.name},
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
        operation_object_code="OBJ_PACKAGE", operation_object_params={"name": pkg_name},
        operation_content_code="LOG_PACKAGE_DELETE", operation_content_params={"name": pkg_name},
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
    # 记录添加前的已关联 content_id，用于计算本次新增
    existing_before = set(
        (await db.execute(
            select(ContentPackage.content_id).where(ContentPackage.package_id == package_id)
        )).scalars().all()
    )

    result = await package_service.add_contents_to_package(db, package_id, body, current_user.username)

    # 新增的内容 = 请求列表 - 已存在
    newly_added = [cid for cid in body.content_ids if cid not in existing_before]

    # 关联服务包属于节点数据变更，回退内容状态（已发布/准备发布等 → InProgress）
    if newly_added:
        from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        for cid in newly_added:
            content = await get_content_by_id(db, cid)
            if content:
                await rollback_after_published_edit(
                    db, cid, content.content_type,
                    current_user.username, "关联服务包",
                )

    # 写入操作日志（每个新增内容一条，带 content_id 以便 Activity Log 展示）
    package_name = (await db.execute(select(Package.name).where(Package.id == package_id))).scalar_one_or_none()

    for cid in newly_added:
        import json as _json
        # 与内容详情页勾选服务包（live.py link_content_packages_api）同构：
        # CONTENT_PACKAGE_LINK + package_names，前端渲染"关联服务包: 包名"
        upd_val = _json.dumps(
            {"package_names": package_name or ''},
            ensure_ascii=False,
        )
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CONTENT_PACKAGE_LINK,
            operation_object_code="log.package.link",
            operation_content_code="log.package.link",
            content_id=cid,
            entity_type="content",
            entity_id=cid,
            updated_value=upd_val,
            updated_value_json=_json.dumps(
                {"content_id": cid, "package_id": package_id},
                ensure_ascii=False,
            ),
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
    await package_service.remove_content_from_package(db, package_id, content_id, current_user.username)

    # 已发布内容取消关联后回滚状态
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
    from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
    content = await get_content_by_id(db, content_id)
    if content:
        await rollback_after_published_edit(
            db, content_id, content.content_type,
            current_user.username, "取消服务包关联",
        )

    # 补写变更前快照（服务包名），与内容详情页取消勾选（live.py unlink_content_package_api）
    # 同构：CONTENT_PACKAGE_UNLINK + package_names，前端渲染"取消关联服务包: 包名"
    import json as _json
    package_name = (await db.execute(select(Package.name).where(Package.id == package_id))).scalar_one_or_none()
    prev_val = _json.dumps(
        {"package_names": package_name or ''},
        ensure_ascii=False,
    )

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
        updated_value_json=_json.dumps(
            {"content_id": content_id, "package_id": package_id},
            ensure_ascii=False,
        ),
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


# ─── Package Sync ──────────────────────────────────────────────────────

class PackageSyncRequest(BaseModel):
    """Package 同步请求体"""
    package_ids: list[int]


class PackageSyncResponse(BaseModel):
    """Package 同步响应"""
    success: bool
    file_path: str
    stats: dict
    synced_ids: list[int]
    message: str
    correlate_id: str | None = None
    soap_success: bool | None = None


# ─── POST /packages/sync ─────────────────────────────────────────────

@router.post("/sync", response_model=PackageSyncResponse)
async def sync_packages_to_business(
    body: PackageSyncRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    将选中的 Package（服务包）同步给业务系统。

    生成符合 C2 规范的 ADI XML，仅包含 Package 对象（Name、Description、Extendinfo），
    无 Mapping 关系。

    同步策略：所有 Package 统一使用 REGIST 操作。
    """
    package_ids = body.package_ids
    if not package_ids:
        return PackageSyncResponse(
            success=False,
            file_path="",
            stats={"regist": 0, "update": 0, "skip": 0},
            synced_ids=[],
            message=get_msg("PACKAGE_IDS_REQUIRED"),
        )

    try:
        builder = PackageSyncBuilder(db)
        result = await builder.build_sync(package_ids)

        # 记录操作日志
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type="PACKAGE_SYNC",
            operation_object_code="OBJ_PACKAGE",
            operation_content_code="LOG_PACKAGE_SYNC",
            operation_content_params={"result": f"ids={package_ids}, stats={result.get('stats', '')}, CorrelateID={result.get('correlate_id', '')}"},
            ip_address=_get_ip(request),
            result="success",
            entity_type="package",
            entity_id=package_ids[0],
        )
        await db.commit()

        stats = result["stats"]
        soap_success = result.get("soap_success", True)
        soap_enabled = result.get("soap_enabled", False)
        correlate_id = result.get("correlate_id", "")

        if soap_success and soap_enabled:
            message = get_msg("PACKAGE_SYNC_SUBMITTED", correlate_id=correlate_id)
        elif soap_success and not soap_enabled:
            message = get_msg("PACKAGE_SYNC_SUCCESS")
        else:
            message = get_msg("PACKAGE_SYNC_SOAP_FAILED", error=result.get('soap_error', get_msg("PACKAGE_SYNC_UNKNOWN_ERROR")))

        return PackageSyncResponse(
            success=soap_success,
            file_path=result["file_path"],
            stats=stats,
            synced_ids=result["synced_ids"],
            message=message,
            correlate_id=correlate_id,
            soap_success=soap_success,
        )

    except Exception as e:
        from loguru import logger
        logger.error(f"[PackageSync] 同步失败: {e}")
        await db.rollback()
        return PackageSyncResponse(
            success=False,
            file_path="",
            stats={"regist": 0, "update": 0, "skip": 0},
            synced_ids=[],
            message=get_msg("PACKAGE_SYNC_FAILED", error=str(e)),
        )
