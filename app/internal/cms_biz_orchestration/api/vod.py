"""
点播管理（VOD）API 路由层。

路由前缀：/vod

接口列表：
    GET  /contents    查询点播内容列表（仅 MOVIE/SEASON/SERIES/EPISODE，分页 + 多维过滤）
    POST /contents/export    导出 VOD 内容 Excel
    POST /contents/import    导入 VOD 内容 Excel
    GET  /contents/template  下载 VOD 导入模板
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_orchestration.schemas.content import VodContentListItem
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_orchestration.services import content_service

router = APIRouter(prefix="/vod")


@router.get("/contents", response_model=PaginatedResponse[VodContentListItem])
async def get_vod_content_list(
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    genre_ids: list[int] | None = Query(default=None),
    custom_tag_ids: list[int] | None = Query(default=None),
    deleted: str | None = None,
    type_ids: list[int] | None = Query(default=None),
    category_name: str | None = None,
    package_ids: list[int] | None = Query(default=None),
    provider_ids: list[int] | None = Query(default=None),
    license_start_from: str | None = None,
    license_start_to: str | None = None,
    license_end_from: str | None = None,
    license_end_to: str | None = None,
    unpublish_from: str | None = None,
    unpublish_to: str | None = None,
    publish_from: str | None = None,
    publish_to: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    查询点播内容列表（分页）。

    仅返回 Content Type 为 MOVIE / SEASON / SERIES / EPISODE 的内容。

    查询参数：
        page                页码
        page_size           每页条数（默认 10）
        title               内容名称关键字（模糊）
        content_types       内容类型过滤（限定在 VOD 类型内）
        statuses            Ingest 状态列表（任一匹配）
        genre_ids           题材 id 列表
        custom_tag_ids      自定义标签 id 列表
        deleted             是否删除（YES/NO）
        type_ids            类型 id 列表
        category_name       栏目名称关键字（模糊）
        package_ids         服务包 id 列表
        provider_ids        供应商 id 列表（通过许可证关联链过滤）
        license_start_from  许可证开始日期范围下限（YYYY-MM-DD）
        license_start_to    许可证开始日期范围上限
        license_end_from    许可证结束日期范围下限
        license_end_to      许可证结束日期范围上限
        unpublish_from      下架日期范围下限（YYYY-MM-DD）
        unpublish_to        下架日期范围上限
        publish_from        发布日期范围下限（YYYY-MM-DD）
        publish_to          发布日期范围上限
        sort_by             排序字段
        sort_order          排序方向（asc/desc）
    """
    return await content_service.list_vod_contents(
        db, page, page_size,
        title, content_types, statuses, genre_ids,
        custom_tag_ids, deleted, type_ids, category_name, package_ids, provider_ids,
        license_start_from, license_start_to,
        license_end_from, license_end_to,
        unpublish_from, unpublish_to,
        publish_from, publish_to,
        sort_by, sort_order,
        current_user=current_user,
    )


# ─── VOD Excel 导出/导入 ───────────────────────────────────────────────

@router.post("/contents/export")
async def export_vod_contents_api(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """导出 VOD 内容为 Excel 文件（含全部字段及关联信息）。"""
    ids = body.get("ids", [])
    data = await content_service.export_vod_contents_excel(db, ids)
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename=vod_contents_{timestamp}.xlsx"},
    )


@router.post("/contents/import", response_model=content_service.VodImportResult)
async def import_vod_contents_api(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """从 Excel 导入 VOD 内容。以 Content ID 为唯一标识，重复导入以最新数据覆盖。"""
    result = await content_service.import_vod_contents_excel(db, file, processed_by=current_user.username)
    return result


@router.get("/contents/template")
async def download_vod_import_template(
    _: User = Depends(get_current_user),
):
    """下载 VOD 内容导入模板。"""
    data = content_service.generate_vod_import_template()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=vod_import_template.xlsx"},
    )
