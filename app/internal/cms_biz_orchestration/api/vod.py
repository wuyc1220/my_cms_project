"""
点播管理（VOD）API 路由层。

路由前缀：/vod

接口列表：
    GET  /contents    查询点播内容列表（仅 MOVIE/SEASON/SERIES/EPISODE，分页 + 多维过滤）
"""

from fastapi import APIRouter, Depends, Query
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
    genre_id: int | None = None,
    provider_id: int | None = None,
    package_name: str | None = None,
    license_start_from: str | None = None,
    license_start_to: str | None = None,
    license_end_from: str | None = None,
    license_end_to: str | None = None,
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
        genre_id            题材 id
        provider_id         供应商 id（通过许可证关联链过滤）
        package_name        服务包名称关键字（通过关联的服务包过滤）
        license_start_from  许可证开始日期范围下限（YYYY-MM-DD）
        license_start_to    许可证开始日期范围上限
        license_end_from    许可证结束日期范围下限
        license_end_to      许可证结束日期范围上限
        sort_by             排序字段
        sort_order          排序方向（asc/desc）
    """
    return await content_service.list_vod_contents(
        db, page, page_size,
        title, content_types, statuses, genre_id,
        provider_id, package_name,
        license_start_from, license_start_to,
        license_end_from, license_end_to,
        sort_by, sort_order,
        current_user=current_user,
    )
