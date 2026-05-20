"""
直播管理（Live）业务逻辑层。

职责：
- 频道管理：查询 CHANNEL 类型内容列表（含服务包/供应商/许可证关联）；频道详情；频道更新
- 节目单管理：查询 SCHEDULE 类型内容列表（含所属频道名称）；新增/删除节目单
- 归档管理：查询 MOVIE/EPISODE/SEASON/SERIES 类型内容列表（同 VOD 视角，含关联信息）
- 物理频道管理：查询/新增/编辑/删除物理频道
- 内容-服务包关联：查询/新增/删除关联
- 内容-栏目关联：查询/新增/删除关联
- 流程/日志：查询流程、状态日志、活动日志

未实现字段（模型尚无对应字段，返回 None 占位）：
- cutv_enable（CUTV 启用）
- archived（是否归档）
- category_name（栏目）
- publish_date / takedown_date（发布/下架日期）
"""

import io
import json
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import UploadFile
from loguru import logger
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import String, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import app_tz
from app.internal.cms_biz_metada.models.basic import Category, Genre, CustomField, EntityFieldValue, CustomTag, ContentType as ContentTypeModel
from app.internal.cms_biz_package.models import ContentType, ContentStatus
from app.internal.cms_biz_package.models.package import Content, ContentPackage, ContentCategory, ContentCustomTag, Package, PhysicalChannel, PhysicalChannelHistory
from app.internal.cms_biz_orchestration.models.content_metadata import ContentMetadata, SeriesMetadata, ChannelMetadata, ScheduleMetadata
from app.internal.cms_biz_scp.models.trade import Contract, License, LicenseContent, Provider
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.models.content_auth import ContentAuth
from app.internal.cms_biz_system.services.data_auth_filter import apply_content_data_auth
from app.internal.cms_biz_orchestration.schemas.live import (
    ArchiveListItem, ChannelListItem, ChannelDetailItem, ChannelUpdate,
    ScheduleCreate, ScheduleListItem, ScheduleImportResult,
    PhysicalChannelListItem, PhysicalChannelCreate,
    PhysicalChannelHistoryItem,
    ContentPackageRef, ContentCategoryRef,
    ProcessListItem, StatusLogListItem, ActivityLogListItem,
    ArchiveRequest, ArchiveResponse,
)
from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
)
from app.internal.cms_biz_package.services.task_service import (
    _ensure_content_auth,
)
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode, ForbiddenException
from app.common.utils import is_admin_user


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _get_genre_name(db: AsyncSession, genre_id: Optional[int]) -> Optional[str]:
    """查询题材名称。"""
    if genre_id is None:
        return None
    return (await db.execute(select(Genre.name).where(Genre.id == genre_id, Genre.is_deleted.is_(False)))).scalar_one_or_none()


async def _get_content_title(db: AsyncSession, content_id: Optional[int]) -> Optional[str]:
    """查询内容标题（用于获取频道名称等）。"""
    if content_id is None:
        return None
    return (
        await db.execute(
            select(Content.title).where(Content.id == content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
        )
    ).scalar_one_or_none()


async def _get_custom_tags(db: AsyncSession, content_id: int) -> tuple[list[int], list[str]]:
    """查询内容关联的自定义标签 ID 列表和名称列表。"""
    rows = (
        await db.execute(
            select(ContentCustomTag.custom_tag_id, CustomTag.name)
            .join(CustomTag, ContentCustomTag.custom_tag_id == CustomTag.id)
            .where(ContentCustomTag.content_id == content_id, CustomTag.is_deleted.is_(False))
        )
    ).all()
    ids = [r.custom_tag_id for r in rows]
    names = [r.name for r in rows]
    return ids, names


async def _build_package_provider_license(
    db: AsyncSession, content_id: int
) -> tuple[list[str], list[str], Optional[str], Optional[str]]:
    """
    为指定内容查询关联服务包名称、供应商名称、许可证日期范围。

    返回：(package_names, provider_names, license_start, license_end)
    """
    # 服务包
    pkg_ids = (
        await db.execute(select(ContentPackage.package_id).where(ContentPackage.content_id == content_id))
    ).scalars().all()
    package_names: list[str] = []
    if pkg_ids:
        names = (
            await db.execute(
                select(Package.name).where(Package.id.in_(pkg_ids), Package.is_deleted.is_(False))
            )
        ).scalars().all()
        package_names = list(names)

    # 许可证 + 供应商
    lic_ids = (
        await db.execute(select(LicenseContent.license_id).where(
            LicenseContent.content_id == content_id,
            LicenseContent.is_deleted.is_(False),
        ))
    ).scalars().all()
    provider_names: list[str] = []
    license_start: Optional[str] = None
    license_end: Optional[str] = None
    if lic_ids:
        lics = (
            await db.execute(
                select(License).where(License.id.in_(lic_ids), License.is_deleted.is_(False))
            )
        ).scalars().all()
        seen_providers: set[int] = set()
        starts = []
        ends = []
        for lic in lics:
            if lic.start_date:
                starts.append(lic.start_date)
            if lic.end_date:
                ends.append(lic.end_date)
            if lic.contract and lic.contract.provider_id not in seen_providers:
                seen_providers.add(lic.contract.provider_id)
                if lic.contract.provider:
                    provider_names.append(lic.contract.provider.name)
        license_start = str(min(starts)) if starts else None
        license_end = str(max(ends)) if ends else None

    return package_names, provider_names, license_start, license_end


# ─── 频道管理（CHANNEL）────────────────────────────────────────────────

async def _build_channel_item(db: AsyncSession, c: Content) -> ChannelListItem:
    """将 ORM Content（CHANNEL 类型）转换为频道列表响应。"""
    genre_name = await _get_genre_name(db, c.genre_id)
    pkg_names, prov_names, lic_start, lic_end = await _build_package_provider_license(db, c.id)

    # 栏目名称
    cat_ids = (
        await db.execute(select(ContentCategory.category_id).where(ContentCategory.content_id == c.id))
    ).scalars().all()
    category_names: list[str] = []
    if cat_ids:
        cat_names = (
            await db.execute(select(Category.name).where(Category.id.in_(cat_ids)))
        ).scalars().all()
        category_names = list(cat_names)

    # 自定义标签名称（与 VOD 内容管理一致，从 content_custom_tag 关联表读取）
    custom_tag_names: list[str] = []
    ct_rows = (
        await db.execute(
            select(CustomTag.name)
            .join(ContentCustomTag, ContentCustomTag.custom_tag_id == CustomTag.id)
            .where(ContentCustomTag.content_id == c.id, CustomTag.is_deleted.is_(False))
        )
    ).scalars().all()
    custom_tag_names = list(ct_rows)

    channel_number: Optional[int] = None
    language: list[str] = []
    meta = (
        await db.execute(
            select(ChannelMetadata).where(ChannelMetadata.content_id == c.id, ChannelMetadata.is_deleted.is_(False), ChannelMetadata.is_discarded.is_(False))
        )
    ).scalar_one_or_none()
    if meta:
        channel_number = meta.channel_number
        language = list(meta.language) if meta.language else []

    return ChannelListItem(
        id=c.id,
        title=c.title,
        status=c.status,
        genre_id=c.genre_id,
        genre_name=genre_name,
        channel_number=channel_number,
        language=language,
        category_names=category_names,
        package_names=pkg_names,
        custom_tag_names=custom_tag_names,
        provider_names=prov_names,
        license_start=lic_start,
        license_end=lic_end,
        created_at=c.created_at,
    )


async def list_channels(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    title: Optional[str] = None,
    statuses: Optional[list[str]] = None,
    genre_id: Optional[int] = None,
    genre_ids: Optional[list[int]] = None,
    provider_id: Optional[int] = None,
    provider_ids: Optional[list[int]] = None,
    package_name: Optional[str] = None,
    package_id: Optional[int] = None,
    package_ids: Optional[list[int]] = None,
    category_id: Optional[int] = None,
    category_name: Optional[str] = None,
    custom_tag_ids: Optional[list[int]] = None,
    channel_number: Optional[str] = None,
    languages: Optional[list[str]] = None,
    license_start_from: Optional[str] = None,
    license_start_to: Optional[str] = None,
    license_end_from: Optional[str] = None,
    license_end_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    current_user: Optional[User] = None,
) -> PaginatedResponse[ChannelListItem]:
    """
    查询频道列表（仅 CHANNEL 类型，分页）。

    输入参数：
        page                页码
        page_size           每页条数（默认 10）
        title               频道名称关键字（模糊）
        statuses            Ingest 状态列表（任一匹配）
        genre_id            题材 id（单选，向下兼容）
        genre_ids           题材 id 列表（多选，任一匹配）
        provider_id         供应商 id（单选，向下兼容）
        provider_ids        供应商 id 列表（多选，任一匹配）
        package_name        服务包名称关键字（通过 content_package 关联）
        package_id          服务包 id（单选，向下兼容）
        package_ids         服务包 id 列表（多选，任一匹配）
        category_id         栏目 id（单选，向下兼容）
        category_name       栏目名称关键字（模糊搜索）
        custom_tag_ids      自定义标签 id 列表（任一匹配，通过 ChannelMetadata）
        channel_number      频道号关键字（模糊，将 ChannelMetadata.channel_number 转为文本匹配）
        languages           频道语言多选（数据字典 Language code，任一匹配，通过 ChannelMetadata.language）
        license_start_from  许可证开始日期范围下限（YYYY-MM-DD）
        license_start_to    许可证开始日期范围上限
        license_end_from    许可证结束日期范围下限
        license_end_to      许可证结束日期范围上限

    输出：
        PaginatedResponse[ChannelListItem]
    """
    logger.info(f"list_channels 入参: page={page}, page_size={page_size}, title={title}, statuses={statuses}, genre_id={genre_id}, genre_ids={genre_ids}, provider_id={provider_id}, provider_ids={provider_ids}, package_name={package_name}, package_id={package_id}, package_ids={package_ids}, category_id={category_id}, category_name={category_name}, custom_tag_ids={custom_tag_ids}, channel_number={channel_number}, languages={languages}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Content).where(
        Content.content_type == ContentType.CHANNEL.value,
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    # 数据权限过滤：admin 不过滤；其他用户仅看自己创建的或被授权的 CHANNEL
    query = await apply_content_data_auth(db, current_user, query)

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if statuses:
        query = query.where(Content.status.in_(statuses))
    # 题材过滤：单选 genre_id 或多选 genre_ids
    _genre_id_list = list(filter(None, ([genre_id] if genre_id else []) + (genre_ids or [])))
    if _genre_id_list:
        query = query.where(Content.genre_id.in_(_genre_id_list))

    # 服务包名称过滤：先查满足条件的 package_id → content_id
    if package_name:
        pkg_ids = (
            await db.execute(
                select(Package.id).where(
                    Package.name.ilike(f"%{package_name}%"),
                    Package.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        if pkg_ids:
            content_ids_with_pkg = select(ContentPackage.content_id).where(
                ContentPackage.package_id.in_(pkg_ids)
            )
            query = query.where(Content.id.in_(content_ids_with_pkg))
        else:
            query = query.where(Content.id == -1)  # 无匹配时返回空结果

    # 服务包 ID 过滤：单选 package_id 或多选 package_ids
    _pkg_id_list = list(filter(None, ([package_id] if package_id else []) + (package_ids or [])))
    if _pkg_id_list:
        content_ids_with_pkg_ids = select(ContentPackage.content_id).where(
            ContentPackage.package_id.in_(_pkg_id_list)
        )
        query = query.where(Content.id.in_(content_ids_with_pkg_ids))

    # 供应商过滤：单选 provider_id 或多选 provider_ids
    _prov_id_list = list(filter(None, ([provider_id] if provider_id else []) + (provider_ids or [])))
    if _prov_id_list:
        contract_ids = (
            await db.execute(
                select(Contract.id).where(Contract.provider_id.in_(_prov_id_list), Contract.is_deleted.is_(False))
            )
        ).scalars().all()
        lic_ids_by_prov: list[int] = []
        if contract_ids:
            lic_ids_by_prov = (
                await db.execute(
                    select(License.id).where(
                        License.contract_id.in_(contract_ids), License.is_deleted.is_(False)
                    )
                )
            ).scalars().all()
        if lic_ids_by_prov:
            content_ids_by_prov = select(LicenseContent.content_id).where(
                LicenseContent.license_id.in_(lic_ids_by_prov),
                LicenseContent.is_deleted.is_(False),
            )
            query = query.where(Content.id.in_(content_ids_by_prov))
        else:
            query = query.where(Content.id == -1)

    # 栏目过滤：category_id 精确匹配 或 category_name 模糊搜索
    if category_id is not None:
        cat_cids = select(ContentCategory.content_id).where(ContentCategory.category_id == category_id)
        query = query.where(Content.id.in_(cat_cids))
    elif category_name:
        # 栏目名称模糊搜索：先查满足条件的 category_id → ContentCategory → content_id
        cat_ids = (
            await db.execute(
                select(Category.id).where(Category.name.ilike(f"%{category_name}%"))
            )
        ).scalars().all()
        if cat_ids:
            cat_cids = select(ContentCategory.content_id).where(ContentCategory.category_id.in_(cat_ids))
            query = query.where(Content.id.in_(cat_cids))
        else:
            query = query.where(Content.id == -1)

    # 自定义标签过滤：custom_tag_ids → content_custom_tag 关联表（任一匹配，与 VOD 内容管理一致）
    if custom_tag_ids:
        ch_cids = select(ContentCustomTag.content_id).where(
            ContentCustomTag.custom_tag_id.in_(custom_tag_ids),
        )
        query = query.where(Content.id.in_(ch_cids))

    # 频道号模糊搜索：channel_number → ChannelMetadata.channel_number::text ilike
    if channel_number:
        ch_cids_num = select(ChannelMetadata.content_id).where(
            func.cast(ChannelMetadata.channel_number, String).ilike(f"%{channel_number}%"),
            ChannelMetadata.is_deleted.is_(False), ChannelMetadata.is_discarded.is_(False),
        )
        query = query.where(Content.id.in_(ch_cids_num))

    # 语言多选：languages → ChannelMetadata.language overlap
    if languages:
        ch_cids_lang = select(ChannelMetadata.content_id).where(
            ChannelMetadata.language.overlap(languages),
            ChannelMetadata.is_deleted.is_(False), ChannelMetadata.is_discarded.is_(False),
        )
        query = query.where(Content.id.in_(ch_cids_lang))

    # 许可证日期范围过滤
    if license_start_from or license_start_to or license_end_from or license_end_to:
        lic_query = select(License.id).where(License.is_deleted.is_(False))
        if license_start_from:
            lic_query = lic_query.where(License.start_date >= date.fromisoformat(license_start_from))
        if license_start_to:
            lic_query = lic_query.where(License.start_date <= date.fromisoformat(license_start_to))
        if license_end_from:
            lic_query = lic_query.where(License.end_date >= date.fromisoformat(license_end_from))
        if license_end_to:
            lic_query = lic_query.where(License.end_date <= date.fromisoformat(license_end_to))
        content_ids_with_lic = select(LicenseContent.content_id).where(
            LicenseContent.license_id.in_(lic_query),
            LicenseContent.is_deleted.is_(False),
        )
        query = query.where(Content.id.in_(content_ids_with_lic))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Content, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Content.id.desc())
        else:
            query = query.order_by(Content.id.desc())
    else:
        query = query.order_by(Content.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    items = [await _build_channel_item(db, c) for c in rows]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


# ─── 节目单管理（SCHEDULE）────────────────────────────────────────────

async def _build_schedule_item(db: AsyncSession, c: Content) -> ScheduleListItem:
    """将 ORM Content（SCHEDULE 类型）转换为节目单列表响应。"""
    channel_name = await _get_content_title(db, c.parent_id)

    # 查询归档产物（source_schedule_id == c.id 的 Content）
    archive_content_id: Optional[int] = None
    archive_content_type: Optional[str] = None
    archive_published: bool = False
    if c.is_archived:
        archive = (
            await db.execute(
                select(Content).where(
                    Content.source_schedule_id == c.id,
                    Content.is_archived.is_(True),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                    Content.content_type.in_(
                        [ContentType.MOVIE.value, ContentType.EPISODE.value]
                    ),
                )
            )
        ).scalars().first()
        if archive:
            archive_content_id = archive.id
            archive_content_type = archive.content_type
            archive_published = archive.status == ContentStatus.PUBLISHED.value

    return ScheduleListItem(
        id=c.id,
        title=c.title,
        status=c.status,
        channel_id=c.parent_id,
        channel_name=channel_name,
        begin_time=c.begin_time,
        end_time=c.end_time,
        cutv_enable=c.cutv_enable,
        is_archived=c.is_archived,
        archive_content_id=archive_content_id,
        archive_content_type=archive_content_type,
        archive_published=archive_published,
        archive_scheduled_time=c.archive_scheduled_time,
        created_at=c.created_at,
    )


async def list_schedules(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    title: Optional[str] = None,
    channel_id: Optional[int] = None,
    channel_name: Optional[str] = None,
    cutv_enable: Optional[bool] = None,
    cutv_enables: Optional[list[str]] = None,
    is_archived: Optional[bool] = None,
    begin_from: Optional[str] = None,
    begin_to: Optional[str] = None,
    end_from: Optional[str] = None,
    end_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    current_user: Optional[User] = None,
) -> PaginatedResponse[ScheduleListItem]:
    """
    查询节目单列表（仅 SCHEDULE 类型，分页）。

    输入参数：
        page            页码
        page_size       每页条数（默认 10）
        title           节目名称关键字（模糊）
        channel_id      所属频道 id（精确匹配 parent_id）
        channel_name    频道名称关键字（模糊，通过 parent 内容标题过滤）
        cutv_enable     CUTV 启用（单选，向下兼容）
        cutv_enables    CUTV 启用多选列表（YES/NO 字符串，任一匹配）
        is_archived     是否已归档（精确匹配）
        begin_from      开始时间下限（YYYY-MM-DD HH:MM）
        begin_to        开始时间上限
        end_from        结束时间下限
        end_to          结束时间上限

    输出：
        PaginatedResponse[ScheduleListItem]
    """
    logger.info(f"list_schedules 入参: page={page}, page_size={page_size}, title={title}, channel_id={channel_id}, channel_name={channel_name}, cutv_enable={cutv_enable}, cutv_enables={cutv_enables}, is_archived={is_archived}, begin_from={begin_from}, begin_to={begin_to}, end_from={end_from}, end_to={end_to}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Content).where(
        Content.content_type == ContentType.SCHEDULE.value,
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    # 数据权限过滤：admin 不过滤；其他用户仅看自己创建的或被授权的 SCHEDULE
    query = await apply_content_data_auth(db, current_user, query)

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if channel_id is not None:
        query = query.where(Content.parent_id == channel_id)
    # CUTV 启用过滤：单选 cutv_enable 或多选 cutv_enables
    if cutv_enable is not None:
        query = query.where(Content.cutv_enable.is_(cutv_enable))
    if cutv_enables:
        # cutv_enables 是 YES/NO 字符串列表，转为 bool 过滤
        _bool_vals = []
        for v in cutv_enables:
            if v.upper() == 'YES':
                _bool_vals.append(True)
            elif v.upper() == 'NO':
                _bool_vals.append(False)
        if _bool_vals:
            query = query.where(Content.cutv_enable.in_(_bool_vals))
    if is_archived is not None:
        query = query.where(Content.is_archived.is_(is_archived))
    if channel_name:
        # 先查满足频道名称的 channel content_id，再过滤 parent_id
        channel_ids = (
            await db.execute(
                select(Content.id).where(
                    Content.content_type == ContentType.CHANNEL.value,
                    Content.title.ilike(f"%{channel_name}%"),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalars().all()
        if channel_ids:
            query = query.where(Content.parent_id.in_(channel_ids))
        else:
            query = query.where(Content.id == -1)
    if begin_from:
        query = query.where(Content.begin_time >= datetime.strptime(begin_from, "%Y-%m-%d %H:%M").replace(tzinfo=app_tz))
    if begin_to:
        query = query.where(Content.begin_time <= datetime.strptime(begin_to, "%Y-%m-%d %H:%M").replace(tzinfo=app_tz))
    if end_from:
        query = query.where(Content.end_time >= datetime.strptime(end_from, "%Y-%m-%d %H:%M").replace(tzinfo=app_tz))
    if end_to:
        query = query.where(Content.end_time <= datetime.strptime(end_to, "%Y-%m-%d %H:%M").replace(tzinfo=app_tz))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Content, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Content.id.desc())
        else:
            query = query.order_by(Content.begin_time.desc())
    else:
        query = query.order_by(Content.begin_time.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    items = [await _build_schedule_item(db, c) for c in rows]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def create_schedule(db: AsyncSession, data: ScheduleCreate) -> ScheduleListItem:
    """
    新增节目单。

    输入：ScheduleCreate（title / parent_id / begin_time / end_time）
    输出：ScheduleListItem

    业务规则：
    - parent_id 必须指向有效的 CHANNEL 类型内容
    - begin_time 必须早于 end_time
    """
    logger.info(f"create_schedule 入参: data={data}")
    # 校验 parent 是否为有效 CHANNEL
    channel = (
        await db.execute(
            select(Content).where(
                Content.id == data.parent_id,
                Content.content_type == ContentType.CHANNEL.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not channel:
        raise BusinessException(ErrorCode.CHANNEL_NOT_FOUND_OR_WRONG_TYPE, get_msg("CHANNEL_NOT_FOUND_OR_WRONG_TYPE"))

    if data.begin_time >= data.end_time:
        raise BusinessException(ErrorCode.START_TIME_BEFORE_END_TIME, get_msg("START_TIME_BEFORE_END_TIME"))

    schedule = Content(
        content_type=ContentType.SCHEDULE.value,
        title=data.title,
        status=ContentStatus.NONE.value,
        parent_id=data.parent_id,
        begin_time=data.begin_time,
        end_time=data.end_time,
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    logger.info(f"创建节目单成功: id={schedule.id}, title={schedule.title}")

    # 创建内容编排任务；若 assign_to 有值则直接分配给指定用户
    try:
        from app.internal.cms_biz_package.models.task import Task, TaskHistory
        now = datetime.now()
        task = Task(
            content_id=schedule.id,
            task_type="arrangement",
            assignee_id=data.assign_to,
            task_status="Pending" if data.assign_to else "Not Assigned",
            start_time=now,
        )
        db.add(task)
        await db.flush()
        db.add(
            TaskHistory(
                task_id=task.id,
                processed_type="Add",
                processed_by="system",
                previous_value=None,
                updated_value=f"Task created (assignee_id={data.assign_to or ''})",
            )
        )
        # 若有负责人，自动添加数据权限
        if data.assign_to:
            existing = (
                await db.execute(
                    select(ContentAuth).where(
                        ContentAuth.content_id == schedule.id,
                        ContentAuth.user_id == data.assign_to,
                        ContentAuth.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if not existing:
                db.add(
                    ContentAuth(
                        content_id=schedule.id,
                        user_id=data.assign_to,
                        role_id=None,
                        is_deleted=False,
                    )
                )
                logger.info(
                    "自动添加数据权限 | content_id={} user_id={}", schedule.id, data.assign_to
                )
        await db.commit()
    except Exception as e:  # noqa: BLE001
        # 任务创建失败不影响节目单主流程
        logger.warning(f"节目单对应编排任务创建失败: schedule_id={schedule.id}, err={e}")

    return await _build_schedule_item(db, schedule)


async def get_schedule(db: AsyncSession, schedule_id: int) -> ScheduleListItem:
    """查询节目单详情。"""
    logger.info(f"get_schedule 入参: schedule_id={schedule_id}")
    schedule = (
        await db.execute(
            select(Content).where(
                Content.id == schedule_id,
                Content.content_type == ContentType.SCHEDULE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not schedule:
        raise NotFoundException(ErrorCode.SCHEDULE_NOT_FOUND, get_msg("SCHEDULE_NOT_FOUND"))
    return await _build_schedule_item(db, schedule)


async def delete_schedule(db: AsyncSession, schedule_id: int) -> None:
    """
    软删除节目单。

    输入：schedule_id
    业务规则：仅允许删除 SCHEDULE 类型内容
    """
    logger.info(f"delete_schedule 入参: schedule_id={schedule_id}")
    schedule = (
        await db.execute(
            select(Content).where(
                Content.id == schedule_id,
                Content.content_type == ContentType.SCHEDULE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not schedule:
        raise NotFoundException(ErrorCode.SCHEDULE_NOT_FOUND, get_msg("SCHEDULE_NOT_FOUND"))
    schedule.is_deleted = True
    await db.commit()


# ─── 节目单 Excel 导出/导入 ─────────────────────────────────────────────

async def export_schedules_excel(db: AsyncSession, ids: list[int]) -> bytes:
    """导出节目单为 Excel 文件。"""
    import openpyxl

    rows = (
        await db.execute(
            select(Content).where(
                Content.id.in_(ids),
                Content.content_type == ContentType.SCHEDULE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().all()

    # 批量获取频道名称
    channel_ids = [r.parent_id for r in rows if r.parent_id]
    channels: dict[int, str] = {}
    if channel_ids:
        ch_rows = (
            await db.execute(
                select(Content).where(Content.id.in_(channel_ids))
            )
        ).scalars().all()
        channels = {c.id: c.title for c in ch_rows}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Schedules"

    headers = ["ID", "Content Type", "Program Name", "Channel Name", "Begin Time", "End Time", "Status", "CUTV Enable", "Archived", "Created At"]
    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    col_widths = [10, 14, 30, 24, 20, 20, 16, 14, 12, 20]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    for row_idx, r in enumerate(rows, 2):
        ws.cell(row=row_idx, column=1, value=r.id)
        ws.cell(row=row_idx, column=2, value="SCHEDULE")
        ws.cell(row=row_idx, column=3, value=r.title)
        ws.cell(row=row_idx, column=4, value=channels.get(r.parent_id, ""))
        ws.cell(row=row_idx, column=5, value=r.begin_time.strftime("%Y-%m-%d %H:%M:%S") if r.begin_time else "")
        ws.cell(row=row_idx, column=6, value=r.end_time.strftime("%Y-%m-%d %H:%M:%S") if r.end_time else "")
        ws.cell(row=row_idx, column=7, value=r.status)
        ws.cell(row=row_idx, column=8, value="YES" if r.cutv_enable else "NO")
        ws.cell(row=row_idx, column=9, value="YES" if r.is_archived else "NO")
        ws.cell(row=row_idx, column=10, value=r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


async def import_schedules_excel(
    db: AsyncSession,
    file: UploadFile,
    force: bool = False,
) -> ScheduleImportResult:
    """从 Excel 导入节目单。

    流程（需求 3.5.2.2 (7)）：
    - 新增记录（无 id 或 id 找不到）时，根据 **频道 + 时间段** 检查冲突；
    - force=False 且发现冲突：不提交任何变更，返回 conflicts 列表，由前端提示用户是否覆盖；
    - force=True：先软删除冲突的 SCHEDULE 记录，再插入新数据。
    """
    content = await file.read()
    wb = load_workbook(io.BytesIO(content), read_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    result = ScheduleImportResult()
    # 预解析：收集有效行 + 冲突检测
    parsed: list[dict] = []  # 有效行 {row, id, title, channel, begin, end}
    conflicts: list = []
    from app.internal.cms_biz_orchestration.schemas.live import ScheduleImportConflict

    for idx, row in enumerate(rows, start=2):
        if not row or not row[2]:
            continue

        id_val = int(row[0]) if row[0] else None
        title = str(row[2]).strip()
        channel_name = str(row[3]).strip() if len(row) > 3 and row[3] else ""
        begin_str = str(row[4]).strip() if len(row) > 4 and row[4] else ""
        end_str = str(row[5]).strip() if len(row) > 5 and row[5] else ""

        if not title or not begin_str or not end_str:
            continue

        try:
            begin_time = datetime.strptime(begin_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=app_tz)
            end_time = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=app_tz)
        except ValueError:
            continue

        if begin_time >= end_time:
            continue

        result.total += 1

        channel = None
        if channel_name:
            channel = (
                await db.execute(
                    select(Content).where(
                        Content.title == channel_name,
                        Content.content_type == ContentType.CHANNEL.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalar_one_or_none()
        if not channel:
            continue

        existing = None
        if id_val:
            existing = (
                await db.execute(
                    select(Content).where(
                        Content.id == id_val,
                        Content.content_type == ContentType.SCHEDULE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalar_one_or_none()

        parsed.append({
            "row": idx,
            "id": id_val,
            "title": title,
            "channel": channel,
            "begin": begin_time,
            "end": end_time,
            "existing": existing,
        })

        # 仅对新增（existing 为空）做冲突检测：同频道且时间段重叠
        if existing is None:
            overlap_rows = (
                await db.execute(
                    select(Content.id, Content.title, Content.begin_time, Content.end_time).where(
                        Content.content_type == ContentType.SCHEDULE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                        Content.parent_id == channel.id,
                        Content.begin_time < end_time,
                        Content.end_time > begin_time,
                    )
                )
            ).all()
            if overlap_rows:
                conflicts.append(
                    ScheduleImportConflict(
                        row=idx,
                        channel_name=channel_name,
                        title=title,
                        begin_time=begin_str,
                        end_time=end_str,
                        conflict_ids=[r[0] for r in overlap_rows],
                    )
                )

    # 有冲突且非强制覆盖→ 不提交，仅返回 conflicts
    if conflicts and not force:
        result.conflicts = conflicts
        result.created = 0
        result.updated = 0
        return result

    # 强制覆盖：先软删除冲突的 SCHEDULE 记录
    if conflicts and force:
        conflict_id_set: set[int] = set()
        for c in conflicts:
            conflict_id_set.update(c.conflict_ids)
        if conflict_id_set:
            victims = (
                await db.execute(
                    select(Content).where(Content.id.in_(conflict_id_set))
                )
            ).scalars().all()
            for v in victims:
                v.is_deleted = True

    # 提交数据
    for item in parsed:
        channel = item["channel"]
        existing = item["existing"]
        if existing is not None:
            existing.title = item["title"]
            existing.parent_id = channel.id
            existing.begin_time = item["begin"]
            existing.end_time = item["end"]
            result.updated += 1
            continue
        db.add(
            Content(
                content_type=ContentType.SCHEDULE.value,
                title=item["title"],
                status=ContentStatus.NONE.value,
                parent_id=channel.id,
                begin_time=item["begin"],
                end_time=item["end"],
            )
        )
        result.created += 1

    await db.commit()

    return result


# ─── 归档管理（已归档的 VOD 内容）───────────────────────────────────────

_ARCHIVE_TYPES = [ContentType.MOVIE.value, ContentType.EPISODE.value, ContentType.SEASON.value, ContentType.SERIES.value]


async def archive_schedule(
    db: AsyncSession,
    data: ArchiveRequest,
) -> ArchiveResponse:
    """
    归档节目单：根据 ScheduleMetadata.series_type 创建对应归档产物。

    归档逻辑（需求 3.5.2 节目归档分支）：
        SeriesType=0 → 创建 MOVIE（is_archived=true, source_schedule_id=schedule.id）
        SeriesType=1 → 查找/创建 SERIES → 在其下创建 EPISODE
        SeriesType=2 → 查找/创建 SEASON → 查找/创建 SERIES → 在其下创建 EPISODE

    元数据继承：
        将 ScheduleMetadata 中的字段复制到归档产物的 ContentMetadata/SeriesMetadata。

    输入：ArchiveRequest
    输出：ArchiveResponse
    """
    logger.info(f"archive_schedule 入参: schedule_id={data.schedule_id}, mode={data.mode}")

    # 1. 查找并校验 SCHEDULE
    schedule = (
        await db.execute(
            select(Content).where(
                Content.id == data.schedule_id,
                Content.content_type == ContentType.SCHEDULE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not schedule:
        raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))

    # 校验 CUTV Enable 是否开启
    if not schedule.cutv_enable:
        raise BusinessException(ErrorCode.CUTV_NOT_ENABLED, get_msg("CUTV_NOT_ENABLED"))

    # 校验是否已归档（已有关联的归档内容）
    existing_archive = (
        await db.execute(
            select(Content).where(
                Content.source_schedule_id == data.schedule_id,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().first()
    if existing_archive:
        return ArchiveResponse(
            success=True,
            schedule_id=data.schedule_id,
            archive_content_id=existing_archive.id,
            archive_content_type=existing_archive.content_type,
            message="Schedule already archived",
        )

    # 计划归档模式：仅设置 archive_scheduled_time，不立即执行归档
    if data.mode == "plan":
        if not data.scheduled_time:
            raise BusinessException(ErrorCode.SCHEDULED_TIME_REQUIRED, get_msg("SCHEDULED_TIME_REQUIRED"))
        schedule.archive_scheduled_time = data.scheduled_time
        await db.flush()
        logger.info(f"计划归档: schedule_id={data.schedule_id}, scheduled_time={data.scheduled_time}")
        return ArchiveResponse(
            success=True,
            schedule_id=data.schedule_id,
            archive_content_id=None,
            archive_content_type=None,
            message="Archive scheduled",
        )

    # 立即归档模式：清除可能存在的计划归档时间
    if schedule.archive_scheduled_time is not None:
        schedule.archive_scheduled_time = None

    # 2. 读取 ScheduleMetadata 获取 series_type 和其他元数据
    sched_meta = (
        await db.execute(
            select(ScheduleMetadata).where(
                ScheduleMetadata.content_id == data.schedule_id,
                ScheduleMetadata.is_deleted.is_(False), ScheduleMetadata.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()

    series_type = sched_meta.series_type if sched_meta else 0
    genre_id = schedule.genre_id

    # 3. 根据 series_type 创建归档产物
    archive_content_id: Optional[int] = None
    archive_content_type: Optional[str] = None

    if series_type == 0:
        # SeriesType=0: 独立 program → 直接创建 MOVIE
        movie = Content(
            content_type=ContentType.MOVIE.value,
            title=schedule.title,
            status=ContentStatus.NONE.value,
            genre_id=genre_id,
            is_archived=True,
            source_schedule_id=data.schedule_id,
        )
        db.add(movie)
        await db.flush()
        archive_content_id = movie.id
        archive_content_type = ContentType.MOVIE.value

        logger.info(f"归档: SeriesType=0, 创建 MOVIE id={movie.id}")

    elif series_type == 1:
        # SeriesType=1: 普通连续剧单集 → 查找/创建 SERIES → 创建 EPISODE
        series_content = await _find_or_create_series(
            db, sched_meta, schedule, data.schedule_id
        )
        ep_sequence = sched_meta.sequence if sched_meta else None
        if ep_sequence is not None:
            await _check_episode_duplicate(db, series_content.id, ep_sequence)
        episode = Content(
            content_type=ContentType.EPISODE.value,
            title=schedule.title,
            status=ContentStatus.NONE.value,
            parent_id=series_content.id,
            genre_id=genre_id,
            sequence=ep_sequence,
            is_archived=True,
            source_schedule_id=data.schedule_id,
        )
        db.add(episode)
        await db.flush()
        archive_content_id = episode.id
        archive_content_type = ContentType.EPISODE.value

        # 自动维护 VolumnCount：统计 SERIES 下 EPISODE 数
        await _update_volume_count(db, series_content.id)

        logger.info(f"归档: SeriesType=1, SERIES id={series_content.id}, EPISODE id={episode.id}")

    elif series_type == 2:
        # SeriesType=2: 分季连续剧单集 → 查找/创建 SEASON → 查找/创建 SERIES → 创建 EPISODE
        season_content = await _find_or_create_season(
            db, sched_meta, schedule, data.schedule_id
        )
        series_content = await _find_or_create_series_under_season(
            db, sched_meta, season_content, schedule, data.schedule_id
        )
        ep_sequence = sched_meta.sequence if sched_meta else None
        if ep_sequence is not None:
            await _check_episode_duplicate(db, series_content.id, ep_sequence)
        episode = Content(
            content_type=ContentType.EPISODE.value,
            title=schedule.title,
            status=ContentStatus.NONE.value,
            parent_id=series_content.id,
            genre_id=genre_id,
            sequence=ep_sequence,
            is_archived=True,
            source_schedule_id=data.schedule_id,
        )
        db.add(episode)
        await db.flush()
        archive_content_id = episode.id
        archive_content_type = ContentType.EPISODE.value

        # 自动维护 VolumnCount：统计 SERIES 下 EPISODE 数
        await _update_volume_count(db, series_content.id)

        logger.info(f"归档: SeriesType=2, SEASON id={season_content.id}, SERIES id={series_content.id}, EPISODE id={episode.id}")
    else:
        raise BusinessException(ErrorCode.INVALID_SERIES_TYPE, get_msg("INVALID_SERIES_TYPE", series_type=series_type))

    # 4. 标记 SCHEDULE 为已归档
    schedule.is_archived = True

    # 5. 为归档产物创建 arrangement 任务
    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(db, archive_content_id)

    await db.flush()
    logger.info(f"归档完成: schedule_id={data.schedule_id}, archive_content_id={archive_content_id}, type={archive_content_type}")

    return ArchiveResponse(
        success=True,
        schedule_id=data.schedule_id,
        archive_content_id=archive_content_id,
        archive_content_type=archive_content_type,
        message="Archive created successfully",
    )


async def _find_content_by_id_or_external_id(
    db: AsyncSession,
    id_str: str,
    content_type: str,
) -> Optional[Content]:
    """按 Content.id 或 Content.external_id 查找内容。

    前端 AutoComplete 选中已有内容时，会将 Content.id 写入 series_id/show_id。
    但手动创建的内容 external_id 可能为 NULL，因此需要同时按 id 和 external_id 查找。

    查找优先级：
    1. 若 id_str 可转为整数，优先按 Content.id 精确匹配
    2. 按 Content.external_id 精确匹配
    """
    conditions = [
        Content.content_type == content_type,
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    ]

    id_int = None
    try:
        id_int = int(id_str)
    except (ValueError, TypeError):
        pass

    if id_int is not None:
        by_id = (
            await db.execute(
                select(Content).where(
                    Content.id == id_int,
                    *conditions,
                )
            )
        ).scalar_one_or_none()
        if by_id:
            return by_id

    by_external_id = (
        await db.execute(
            select(Content).where(
                Content.external_id == id_str,
                *conditions,
            )
        )
    ).scalar_one_or_none()
    return by_external_id


async def _check_episode_duplicate(
    db: AsyncSession,
    series_id: int,
    sequence: int,
) -> None:
    """校验同一 SERIES 下是否已存在相同集序号的 EPISODE，存在则抛出异常。"""
    existing = (
        await db.execute(
            select(Content.id).where(
                Content.parent_id == series_id,
                Content.content_type == ContentType.EPISODE.value,
                Content.sequence == sequence,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.EPISODE_SEQUENCE_EXISTS, get_msg("EPISODE_SEQUENCE_EXISTS"))


async def _find_or_create_series(
    db: AsyncSession,
    sched_meta: Optional[ScheduleMetadata],
    schedule: Content,
    source_schedule_id: int,
) -> Content:
    """SeriesType=1: 根据 series_id 查找已有 SERIES，不存在则创建。

    查找策略：优先按 Content.id（用户选中已有内容时 series_id 存的是 Content.id），
    其次按 Content.external_id 匹配，解决手动创建的内容 external_id 为 NULL 找不到的问题。
    """
    series_name = sched_meta.series_name if sched_meta else None
    series_id_str = sched_meta.series_id if sched_meta else None

    existing = None
    if series_id_str:
        existing = await _find_content_by_id_or_external_id(
            db, series_id_str, ContentType.SERIES.value
        )

    if existing:
        return existing

    series = Content(
        content_type=ContentType.SERIES.value,
        title=series_name or schedule.title,
        status=ContentStatus.NONE.value,
        series_type=1,
        genre_id=schedule.genre_id,
        is_archived=True,
        source_schedule_id=source_schedule_id,
        external_id=series_id_str,
    )
    db.add(series)
    await db.flush()

    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(db, series.id)

    return series


async def _find_or_create_season(
    db: AsyncSession,
    sched_meta: Optional[ScheduleMetadata],
    schedule: Content,
    source_schedule_id: int,
) -> Content:
    """SeriesType=2: 根据 show_id 查找已有 SEASON（总季连续剧），不存在则创建。

    查找策略：优先按 Content.id（用户选中已有内容时 show_id 存的是 Content.id），
    其次按 Content.external_id 匹配，解决手动创建的内容 external_id 为 NULL 找不到的问题。
    """
    show_name = sched_meta.show_name if sched_meta else None
    show_id_str = sched_meta.show_id if sched_meta else None

    existing = None
    if show_id_str:
        existing = await _find_content_by_id_or_external_id(
            db, show_id_str, ContentType.SEASON.value
        )

    if existing:
        return existing

    # 创建新 SEASON（总季连续剧）
    season = Content(
        content_type=ContentType.SEASON.value,
        title=show_name or schedule.title,
        status=ContentStatus.NONE.value,
        series_type=3,
        genre_id=schedule.genre_id,
        is_archived=True,
        source_schedule_id=source_schedule_id,
        external_id=show_id_str,
    )
    db.add(season)
    await db.flush()

    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(db, season.id)

    return season


async def _find_or_create_series_under_season(
    db: AsyncSession,
    sched_meta: Optional[ScheduleMetadata],
    season: Content,
    schedule: Content,
    source_schedule_id: int,
) -> Content:
    """SeriesType=2: 根据 series_id 在 SEASON 下查找/创建单季连续剧 SERIES。

    查找策略：优先按 Content.id（用户选中已有内容时 series_id 存的是 Content.id），
    其次按 Content.external_id 匹配，解决手动创建的内容 external_id 为 NULL 找不到的问题。
    """
    series_name = sched_meta.series_name if sched_meta else None
    series_id_str = sched_meta.series_id if sched_meta else None
    series_ordinal = sched_meta.series_ordinal if sched_meta else None

    existing = None
    if series_id_str:
        existing = await _find_content_by_id_or_external_id(
            db, series_id_str, ContentType.SERIES.value
        )

    if existing:
        return existing

    # 创建新 SERIES（单季连续剧）
    series = Content(
        content_type=ContentType.SERIES.value,
        title=series_name or f"{season.title} S{(series_ordinal or 1):02d}",
        status=ContentStatus.NONE.value,
        parent_id=season.id,
        series_type=2,
        genre_id=schedule.genre_id,
        series_ordinal=series_ordinal,
        is_archived=True,
        source_schedule_id=source_schedule_id,
        external_id=series_id_str,
    )
    db.add(series)
    await db.flush()

    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(db, series.id)

    return series


async def _update_volume_count(db: AsyncSession, series_content_id: int) -> None:
    """自动维护 VolumnCount：统计 SERIES 下的 EPISODE 数量并回写。"""
    from sqlalchemy import func as sa_func

    episode_count = (
        await db.execute(
            select(sa_func.count()).where(
                Content.parent_id == series_content_id,
                Content.content_type == ContentType.EPISODE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar() or 0

    # 更新 SeriesMetadata 的 volume_count
    series_meta = (
        await db.execute(
            select(SeriesMetadata).where(SeriesMetadata.content_id == series_content_id, SeriesMetadata.is_deleted.is_(False), SeriesMetadata.is_discarded.is_(False))
        )
    ).scalar_one_or_none()
    if series_meta:
        series_meta.volume_count = episode_count
        await db.flush()
        logger.info(f"自动维护 volume_count: series_id={series_content_id}, count={episode_count}")


async def _build_archive_item(db: AsyncSession, c: Content) -> ArchiveListItem:
    """将 ORM Content（VOD 类型）转换为归档列表响应。"""
    genre_name = await _get_genre_name(db, c.genre_id)
    pkg_names, prov_names, lic_start, lic_end = await _build_package_provider_license(db, c.id)

    # 栏目名称
    cat_ids = (
        await db.execute(select(ContentCategory.category_id).where(ContentCategory.content_id == c.id))
    ).scalars().all()
    category_names: list[str] = []
    if cat_ids:
        cat_names = (
            await db.execute(select(Category.name).where(Category.id.in_(cat_ids)))
        ).scalars().all()
        category_names = list(cat_names)

    # 自定义标签名称（统一从 content_custom_tag 中间表查询）
    custom_tag_names: list[str] = []
    custom_tag_ids, tag_names = await _get_custom_tags(db, c.id)
    custom_tag_names = tag_names

    # 查询类型名称（从元数据表中获取）
    type_name: Optional[str] = None
    if c.content_type in (ContentType.MOVIE.value, ContentType.EPISODE.value):
        meta = (
            await db.execute(
                select(ContentMetadata.type_id).where(ContentMetadata.content_id == c.id)
            )
        ).scalar_one_or_none()
        if meta:
            type_name = (
                await db.execute(
                    select(ContentTypeModel.name).where(ContentTypeModel.id == meta)
                )
            ).scalar_one_or_none()
    elif c.content_type in (ContentType.SERIES.value, ContentType.SEASON.value):
        meta = (
            await db.execute(
                select(SeriesMetadata.type_id).where(SeriesMetadata.content_id == c.id)
            )
        ).scalar_one_or_none()
        if meta:
            type_name = (
                await db.execute(
                    select(ContentTypeModel.name).where(ContentTypeModel.id == meta)
                )
            ).scalar_one_or_none()

    # 归档来源信息：通过 source_schedule_id → SCHEDULE → CHANNEL 获取频道名/播出时间
    channel_name: Optional[str] = None
    begin_time_val: Optional[datetime] = None
    end_time_val: Optional[datetime] = None
    if c.source_schedule_id:
        schedule_src = (
            await db.execute(
                select(Content).where(Content.id == c.source_schedule_id, Content.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if schedule_src:
            begin_time_val = schedule_src.begin_time
            end_time_val = schedule_src.end_time
            if schedule_src.parent_id:
                channel_name = await _get_content_title(db, schedule_src.parent_id)

    return ArchiveListItem(
        id=c.id,
        content_type=c.content_type,
        title=c.title,
        status=c.status,
        genre_id=c.genre_id,
        genre_name=genre_name,
        type_name=type_name,
        channel_name=channel_name,
        begin_time=begin_time_val,
        end_time=end_time_val,
        category_names=category_names,
        package_names=pkg_names,
        custom_tag_names=custom_tag_names,
        provider_names=prov_names,
        license_start=lic_start,
        license_end=lic_end,
        sequence=c.sequence,
        series_ordinal=c.series_ordinal,
        created_at=c.created_at,
    )


async def list_archives(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    title: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    statuses: Optional[list[str]] = None,
    genre_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    package_id: Optional[int] = None,
    category_id: Optional[int] = None,
    custom_tag_ids: Optional[list[int]] = None,
    channel_name: Optional[str] = None,
    program_name: Optional[str] = None,
    begin_time_from: Optional[str] = None,
    begin_time_to: Optional[str] = None,
    end_time_from: Optional[str] = None,
    end_time_to: Optional[str] = None,
    license_start_from: Optional[str] = None,
    license_start_to: Optional[str] = None,
    license_end_from: Optional[str] = None,
    license_end_to: Optional[str] = None,
    source_schedule_id: Optional[int] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    current_user: Optional[User] = None,
) -> PaginatedResponse[ArchiveListItem]:
    """
    查询归档内容列表（仅 MOVIE/EPISODE/SEASON/SERIES 类型，分页）。

    输入参数：
        page                页码
        page_size           每页条数（默认 10）
        title               节目名称关键字（模糊）
        content_types       内容类型过滤（限定在归档类型内，默认全选）
        statuses            Ingest 状态列表（任一匹配）
        genre_id            题材 id
        provider_id         供应商 id（通过 license 链路过滤）
        package_id          服务包 id（通过 ContentPackage 关联过滤）
        category_id         栏目 id（通过 ContentCategory 关联过滤）
        custom_tag_ids      自定义标签 id 列表（任一匹配；MOVIE/EPISODE 走 program_metadata，SERIES/SEASON 走 series_metadata）
        channel_name        归档来源频道名称关键字（模糊，通过 source_schedule_id → SCHEDULE → CHANNEL 过滤）
        program_name        归档来源节目单名称关键字（模糊，通过 source_schedule_id → SCHEDULE.title 过滤）
        begin_time_from     播出开始时间范围下限（通过 source_schedule_id → SCHEDULE.begin_time 过滤）
        begin_time_to       播出开始时间范围上限
        end_time_from       播出结束时间范围下限（通过 source_schedule_id → SCHEDULE.end_time 过滤）
        end_time_to         播出结束时间范围上限
        license_start_from  许可证开始日期范围下限
        license_start_to    许可证开始日期范围上限
        license_end_from    许可证结束日期范围下限
        license_end_to      许可证结束日期范围上限

    输出：
        PaginatedResponse[ArchiveListItem]

    过滤规则：
        - content_type IN (MOVIE/EPISODE/SEASON/SERIES)
        - is_archived = true（仅归档内容，普通 VOD 内容不在此列）

    TODO（需求 3.5.3.2，依赖后续模块）：
        - publish/unpublish 发布/下架日期 —— 需发布管理模块落库 publish_date/takedown_date
    """
    logger.info(f"list_archives 入参: page={page}, page_size={page_size}, title={title}, content_types={content_types}, statuses={statuses}, genre_id={genre_id}, provider_id={provider_id}, package_id={package_id}, category_id={category_id}, custom_tag_ids={custom_tag_ids}, channel_name={channel_name}, program_name={program_name}, begin_time_from={begin_time_from}, begin_time_to={begin_time_to}, end_time_from={end_time_from}, end_time_to={end_time_to}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, sort_by={sort_by}, sort_order={sort_order}")
    # 限制 content_types 只能是归档类型
    allowed = set(_ARCHIVE_TYPES)
    effective_types = [t for t in (content_types or [])] if content_types else _ARCHIVE_TYPES
    effective_types = [t for t in effective_types if t in allowed] or _ARCHIVE_TYPES

    query = select(Content).where(
        Content.content_type.in_(effective_types),
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
        Content.is_archived.is_(True),
    )
    # 数据权限过滤：admin 不过滤；其他用户仅看自己创建的或被授权的归档内容
    query = await apply_content_data_auth(db, current_user, query)

    if source_schedule_id is not None:
        # 直接归档产物
        direct_ids = set(
            (
                await db.execute(
                    select(Content.id).where(
                        Content.source_schedule_id == source_schedule_id,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                        Content.is_archived.is_(True),
                    )
                )
            ).scalars().all()
        )
        # 向上追溯父级归档链路（EPISODE → SERIES → SEASON），以便第二次归档复用已有父级时详情页仍能展示完整层级
        all_ids: set[int] = set(direct_ids)
        frontier: set[int] = set(direct_ids)
        for _ in range(5):  # 实际最多 2 层，上限为防御
            if not frontier:
                break
            parent_rows = (
                await db.execute(
                    select(Content.parent_id).where(
                        Content.id.in_(frontier),
                        Content.parent_id.is_not(None),
                    )
                )
            ).scalars().all()
            parent_ids = {pid for pid in parent_rows if pid and pid not in all_ids}
            if not parent_ids:
                break
            valid_parents = set(
                (
                    await db.execute(
                        select(Content.id).where(
                            Content.id.in_(parent_ids),
                            Content.is_archived.is_(True),
                            Content.is_deleted.is_(False),
                            Content.is_discarded.is_(False),
                        )
                    )
                ).scalars().all()
            )
            if not valid_parents:
                break
            all_ids.update(valid_parents)
            frontier = valid_parents
        if all_ids:
            query = query.where(Content.id.in_(all_ids))
        else:
            query = query.where(Content.id == -1)

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if statuses:
        query = query.where(Content.status.in_(statuses))
    if genre_id is not None:
        query = query.where(Content.genre_id == genre_id)

    if package_id is not None:
        cids = select(ContentPackage.content_id).where(ContentPackage.package_id == package_id)
        query = query.where(Content.id.in_(cids))

    if category_id is not None:
        cat_cids = select(ContentCategory.content_id).where(ContentCategory.category_id == category_id)
        query = query.where(Content.id.in_(cat_cids))

    if custom_tag_ids:
        query = query.where(
            Content.id.in_(
                select(ContentCustomTag.content_id).where(
                    ContentCustomTag.custom_tag_id.in_(custom_tag_ids),
                )
            )
        )

    # 频道名称过滤：channel_name → source_schedule_id → SCHEDULE → parent_id → CHANNEL
    if channel_name:
        channel_ids = (
            await db.execute(
                select(Content.id).where(
                    Content.content_type == ContentType.CHANNEL.value,
                    Content.title.ilike(f"%{channel_name}%"),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalars().all()
        if channel_ids:
            schedule_ids = (
                await db.execute(
                    select(Content.id).where(
                        Content.content_type == ContentType.SCHEDULE.value,
                        Content.parent_id.in_(channel_ids),
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            if schedule_ids:
                query = query.where(Content.source_schedule_id.in_(schedule_ids))
            else:
                query = query.where(Content.id == -1)
        else:
            query = query.where(Content.id == -1)

    # 节目单名称过滤：program_name → source_schedule_id → SCHEDULE.title
    if program_name:
        schedule_ids_by_name = (
            await db.execute(
                select(Content.id).where(
                    Content.content_type == ContentType.SCHEDULE.value,
                    Content.title.ilike(f"%{program_name}%"),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalars().all()
        if schedule_ids_by_name:
            query = query.where(Content.source_schedule_id.in_(schedule_ids_by_name))
        else:
            query = query.where(Content.id == -1)

    # 播出时间范围过滤：通过 source_schedule_id → SCHEDULE.begin_time / end_time
    if begin_time_from or begin_time_to or end_time_from or end_time_to:
        sched_time_query = select(Content.id).where(
            Content.content_type == ContentType.SCHEDULE.value,
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),
        )
        if begin_time_from:
            dt = datetime.fromisoformat(begin_time_from)
            sched_time_query = sched_time_query.where(Content.begin_time >= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        if begin_time_to:
            dt = datetime.fromisoformat(begin_time_to)
            sched_time_query = sched_time_query.where(Content.begin_time <= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        if end_time_from:
            dt = datetime.fromisoformat(end_time_from)
            sched_time_query = sched_time_query.where(Content.end_time >= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        if end_time_to:
            dt = datetime.fromisoformat(end_time_to)
            sched_time_query = sched_time_query.where(Content.end_time <= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        sched_time_ids = (await db.execute(sched_time_query)).scalars().all()
        if sched_time_ids:
            query = query.where(Content.source_schedule_id.in_(sched_time_ids))
        else:
            query = query.where(Content.id == -1)

    if provider_id is not None:
        contract_ids = (
            await db.execute(
                select(Contract.id).where(Contract.provider_id == provider_id, Contract.is_deleted.is_(False))
            )
        ).scalars().all()
        lic_ids_prov: list[int] = []
        if contract_ids:
            lic_ids_prov = (
                await db.execute(
                    select(License.id).where(
                        License.contract_id.in_(contract_ids), License.is_deleted.is_(False)
                    )
                )
            ).scalars().all()
        if lic_ids_prov:
            cids_prov = select(LicenseContent.content_id).where(
                LicenseContent.license_id.in_(lic_ids_prov),
                LicenseContent.is_deleted.is_(False),
            )
            query = query.where(Content.id.in_(cids_prov))
        else:
            query = query.where(Content.id == -1)

    if license_start_from or license_start_to or license_end_from or license_end_to:
        lic_query = select(License.id).where(License.is_deleted.is_(False))
        if license_start_from:
            lic_query = lic_query.where(License.start_date >= date.fromisoformat(license_start_from))
        if license_start_to:
            lic_query = lic_query.where(License.start_date <= date.fromisoformat(license_start_to))
        if license_end_from:
            lic_query = lic_query.where(License.end_date >= date.fromisoformat(license_end_from))
        if license_end_to:
            lic_query = lic_query.where(License.end_date <= date.fromisoformat(license_end_to))
        cids_lic = select(LicenseContent.content_id).where(
            LicenseContent.license_id.in_(lic_query),
            LicenseContent.is_deleted.is_(False),
        )
        query = query.where(Content.id.in_(cids_lic))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Content, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Content.id.desc())
        else:
            query = query.order_by(Content.id.desc())
    elif source_schedule_id is not None:
        # 节目单详情页归档 Tab 视角：按 SEASON → SERIES → EPISODE → MOVIE 层级 + 季号/集号 展示完整归档链路
        type_order = case(
            (Content.content_type == ContentType.SEASON.value, 0),
            (Content.content_type == ContentType.SERIES.value, 1),
            (Content.content_type == ContentType.EPISODE.value, 2),
            (Content.content_type == ContentType.MOVIE.value, 3),
            else_=9,
        )
        query = query.order_by(
            type_order.asc(),
            Content.series_ordinal.asc().nulls_last(),
            Content.sequence.asc().nulls_last(),
            Content.id.asc(),
        )
    else:
        query = query.order_by(Content.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    items = [await _build_archive_item(db, c) for c in rows]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


# ─── 频道详情 ───────────────────────────────────────────────────────────

async def _build_channel_detail(db: AsyncSession, c: Content) -> ChannelDetailItem:
    """将 ORM Content（CHANNEL 类型）转换为频道详情响应。"""
    genre_name = await _get_genre_name(db, c.genre_id)
    pkg_names, prov_names, lic_start, lic_end = await _build_package_provider_license(db, c.id)

    # 栏目名称
    cat_ids = (
        await db.execute(select(ContentCategory.category_id).where(ContentCategory.content_id == c.id))
    ).scalars().all()
    category_names: list[str] = []
    if cat_ids:
        cat_names = (
            await db.execute(
                select(Category.name).where(Category.id.in_(cat_ids))
            )
        ).scalars().all()
        category_names = list(cat_names)

    # 物理频道数量
    pc_count = (
        await db.execute(
            select(func.count()).where(
                PhysicalChannel.channel_id == c.id,
                PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False),
            )
        )
    ).scalar_one()

    # 节目单数量
    schedule_count = (
        await db.execute(
            select(func.count()).where(
                Content.content_type == ContentType.SCHEDULE.value,
                Content.parent_id == c.id,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one()

    return ChannelDetailItem(
        id=c.id,
        title=c.title,
        content_type=c.content_type,
        status=c.status,
        genre_id=c.genre_id,
        genre_name=genre_name,
        package_names=pkg_names,
        category_names=category_names,
        provider_names=prov_names,
        license_start=lic_start,
        license_end=lic_end,
        physical_channel_count=pc_count,
        schedule_count=schedule_count,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


async def get_channel(db: AsyncSession, channel_id: int) -> ChannelDetailItem:
    """
    查询频道详情。

    输入：channel_id
    输出：ChannelDetailItem
    """
    logger.info(f"get_channel 入参: channel_id={channel_id}")
    channel = (
        await db.execute(
            select(Content).where(
                Content.id == channel_id,
                Content.content_type == ContentType.CHANNEL.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not channel:
        raise NotFoundException(ErrorCode.CHANNEL_NOT_FOUND, get_msg("CHANNEL_NOT_FOUND"))
    return await _build_channel_detail(db, channel)


async def update_channel(db: AsyncSession, channel_id: int, data: ChannelUpdate) -> ChannelDetailItem:
    """
    编辑频道。

    输入：channel_id, ChannelUpdate
    输出：ChannelDetailItem
    """
    logger.info(f"update_channel 入参: channel_id={channel_id}, data={data}")
    channel = (
        await db.execute(
            select(Content).where(
                Content.id == channel_id,
                Content.content_type == ContentType.CHANNEL.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not channel:
        raise NotFoundException(ErrorCode.CHANNEL_NOT_FOUND, get_msg("CHANNEL_NOT_FOUND"))
    if data.title is not None:
        channel.title = data.title
    if data.genre_id is not None:
        channel.genre_id = data.genre_id
    await db.commit()
    await db.refresh(channel)
    return await _build_channel_detail(db, channel)


# ─── 物理频道管理 ─────────────────────────────────────────────────────────

async def list_physical_channels(
    db: AsyncSession,
    channel_id: int,
    page: int = 1,
    page_size: int = 10,
) -> PaginatedResponse[PhysicalChannelListItem]:
    """
    查询物理频道列表。

    输入：channel_id（业务频道 id）, page, page_size
    输出：PaginatedResponse[PhysicalChannelListItem]
    """
    logger.info(f"list_physical_channels 入参: channel_id={channel_id}, page={page}, page_size={page_size}")
    # 校验业务频道存在
    channel = (
        await db.execute(
            select(Content.id).where(
                Content.id == channel_id,
                Content.content_type == "CHANNEL",
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not channel:
        raise NotFoundException(ErrorCode.CHANNEL_NOT_FOUND, get_msg("CHANNEL_NOT_FOUND"))

    query = select(PhysicalChannel).where(
        PhysicalChannel.channel_id == channel_id,
        PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False),
    )
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.order_by(PhysicalChannel.id.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    # 批量查询当前页物理频道的自定义字段值
    pc_ids = [pc.id for pc in rows]
    field_values_map: dict[int, dict[str, Optional[str]]] = {pc_id: {} for pc_id in pc_ids}
    if pc_ids:
        # 获取 custom_field_id -> field_code 映射
        cf_rows = (
            await db.execute(select(CustomField.id, CustomField.field_code).where(CustomField.is_deleted.is_(False)))
        ).all()
        cf_code_map = {cf_id: code for cf_id, code in cf_rows}

        # 获取 entity_field_value 数据
        ev_rows = (
            await db.execute(
                select(EntityFieldValue.entity_id, EntityFieldValue.custom_field_id, EntityFieldValue.value)
                .where(
                    EntityFieldValue.entity_type == "PhysicalChannel",
                    EntityFieldValue.entity_id.in_(pc_ids),
                )
            )
        ).all()
        for entity_id, custom_field_id, value in ev_rows:
            code = cf_code_map.get(custom_field_id)
            if code:
                field_values_map[entity_id][code] = value

    items = [
        PhysicalChannelListItem(
            id=pc.id,
            channel_id=pc.channel_id,
            name=pc.name,
            channel_number=pc.channel_number,
            status=pc.status,
            mediaservice=pc.mediaservice,
            definition=pc.definition,
            videoencode=pc.videoencode,
            bitrate=pc.bitrate,
            deeplink_ch_url=pc.deeplink_ch_url,
            shifttime=pc.shifttime,
            tvod_save_time=pc.tvod_save_time,
            tvod_enable=pc.tvod_enable,
            tstv_enable=pc.tstv_enable,
            cutv_enable=pc.cutv_enable,
            encryption=pc.encryption,
            field_values=field_values_map.get(pc.id, {}),
            created_at=pc.created_at,
            updated_at=pc.updated_at,
        )
        for pc in rows
    ]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def _create_physical_channel_history(
    db: AsyncSession,
    channel_id: int,
    pc_id: int | None,
    data: dict,
    processed_type: str,
    processed_by: str | None,
) -> None:
    """创建物理频道操作历史记录"""
    history = PhysicalChannelHistory(
        physical_channel_id=pc_id,
        channel_id=channel_id,
        name=data.get('name'),
        channel_number=data.get('channel_number'),
        status=data.get('status'),
        mediaservice=data.get('mediaservice'),
        definition=data.get('definition'),
        videoencode=data.get('videoencode'),
        bitrate=data.get('bitrate'),
        deeplink_ch_url=data.get('deeplink_ch_url'),
        shifttime=data.get('shifttime'),
        tvod_save_time=data.get('tvod_save_time'),
        tvod_enable=data.get('tvod_enable'),
        tstv_enable=data.get('tstv_enable'),
        cutv_enable=data.get('cutv_enable'),
        encryption=data.get('encryption'),
        processed_type=processed_type,
        processed_by=processed_by,
    )
    db.add(history)


async def create_physical_channel(
    db: AsyncSession,
    channel_id: int,
    data: PhysicalChannelCreate,
    processed_by: str | None = None,
) -> PhysicalChannelListItem:
    """
    新增物理频道。

    输入：channel_id, PhysicalChannelCreate
    输出：PhysicalChannelListItem
    """
    logger.info(f"create_physical_channel 入参: channel_id={channel_id}, data={data}, processed_by={processed_by}")
    # 校验业务频道存在
    channel = (
        await db.execute(
            select(Content.id).where(
                Content.id == channel_id,
                Content.content_type == "CHANNEL",
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not channel:
        raise NotFoundException(ErrorCode.CHANNEL_NOT_FOUND, get_msg("CHANNEL_NOT_FOUND"))

    pc = PhysicalChannel(
        channel_id=channel_id,
        name=data.name,
        channel_number=data.channel_number,
        status=data.status,
        mediaservice=data.mediaservice,
        definition=data.definition,
        videoencode=data.videoencode,
        bitrate=data.bitrate,
        deeplink_ch_url=data.deeplink_ch_url,
        shifttime=data.shifttime,
        tvod_save_time=data.tvod_save_time,
        tvod_enable=data.tvod_enable,
        tstv_enable=data.tstv_enable,
        cutv_enable=data.cutv_enable,
        encryption=data.encryption,
    )
    db.add(pc)
    await db.flush()  # 获取 pc.id

    # 记录历史
    history_data = {
        "name": data.name,
        "channel_number": data.channel_number,
        "status": data.status,
        "mediaservice": data.mediaservice,
        "definition": data.definition,
        "videoencode": data.videoencode,
        "bitrate": data.bitrate,
        "deeplink_ch_url": data.deeplink_ch_url,
        "shifttime": data.shifttime,
        "tvod_save_time": data.tvod_save_time,
        "tvod_enable": data.tvod_enable,
        "tstv_enable": data.tstv_enable,
        "cutv_enable": data.cutv_enable,
        "encryption": data.encryption,
    }
    await _create_physical_channel_history(db, channel_id, pc.id, history_data, "Add", processed_by)

    # 完成流程节点并更新内容状态
    channel_content = (
        await db.execute(
            select(Content).where(Content.id == channel_id)
        )
    ).scalar_one_or_none()
    if channel_content:
        await complete_process_and_update_status(
            db,
            content_id=channel_id,
            content_type=channel_content.content_type,
            process_name="InjectSubContent",
            processed_by=processed_by,
            info=f"绑定物理频道: {pc.name}",
        )

    await db.commit()
    await db.refresh(pc)
    logger.info(f"create_physical_channel 出参: result={pc}")
    return PhysicalChannelListItem(
        id=pc.id,
        channel_id=pc.channel_id,
        name=pc.name,
        channel_number=pc.channel_number,
        status=pc.status,
        mediaservice=pc.mediaservice,
        definition=pc.definition,
        videoencode=pc.videoencode,
        bitrate=pc.bitrate,
        deeplink_ch_url=pc.deeplink_ch_url,
        shifttime=pc.shifttime,
        tvod_save_time=pc.tvod_save_time,
        tvod_enable=pc.tvod_enable,
        tstv_enable=pc.tstv_enable,
        cutv_enable=pc.cutv_enable,
        encryption=pc.encryption,
        created_at=pc.created_at,
        updated_at=pc.updated_at,
    )


async def delete_physical_channel(
    db: AsyncSession,
    channel_id: int,
    pc_id: int,
    processed_by: str | None = None,
) -> None:
    """
    软删除物理频道。

    输入：channel_id, pc_id
    """
    logger.info(f"delete_physical_channel 入参: channel_id={channel_id}, pc_id={pc_id}, processed_by={processed_by}")
    pc = (
        await db.execute(
            select(PhysicalChannel).where(
                PhysicalChannel.id == pc_id,
                PhysicalChannel.channel_id == channel_id,
                PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not pc:
        raise NotFoundException(ErrorCode.PHYSICAL_CHANNEL_NOT_FOUND, get_msg("PHYSICAL_CHANNEL_NOT_FOUND"))

    # 记录删除历史
    history_data = {
        "name": pc.name,
        "channel_number": pc.channel_number,
        "status": pc.status,
        "mediaservice": pc.mediaservice,
        "definition": pc.definition,
        "videoencode": pc.videoencode,
        "bitrate": pc.bitrate,
        "deeplink_ch_url": pc.deeplink_ch_url,
        "shifttime": pc.shifttime,
        "tvod_save_time": pc.tvod_save_time,
        "tvod_enable": pc.tvod_enable,
        "tstv_enable": pc.tstv_enable,
        "cutv_enable": pc.cutv_enable,
        "encryption": pc.encryption,
    }
    await _create_physical_channel_history(db, channel_id, pc_id, history_data, "Delete", processed_by)

    # 检查频道下是否还有其他物理频道
    remaining_count = (
        await db.execute(
            select(func.count()).select_from(
                select(PhysicalChannel).where(
                    PhysicalChannel.channel_id == channel_id,
                    PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False),
                    PhysicalChannel.id != pc_id,
                ).subquery()
            )
        )
    ).scalar_one()

    # 如果删除后频道下没有物理频道了，记录流程（解绑）
    if remaining_count == 0:
        channel_content = (
            await db.execute(
                select(Content).where(Content.id == channel_id)
            )
        ).scalar_one_or_none()
        if channel_content:
            await complete_process_and_update_status(
                db,
                content_id=channel_id,
                content_type=channel_content.content_type,
                process_name="InjectSubContent",
                processed_by=processed_by,
                info=f"解绑物理频道: {pc.name}",
            )

    pc.is_deleted = True
    await db.commit()


async def list_physical_channel_history(
    db: AsyncSession,
    channel_id: int,
    page: int = 1,
    page_size: int = 10,
    processed_type: str | None = None,
    processed_by: str | None = None,
) -> PaginatedResponse[PhysicalChannelHistoryItem]:
    """
    查询物理频道操作历史记录。
    只查询未被废弃的物理频道的历史记录（关联 PhysicalChannel 表过滤 is_discarded=False）。

    输入：channel_id, page, page_size, processed_type, processed_by
    输出：PaginatedResponse[PhysicalChannelHistoryItem]
    """
    logger.info(f"list_physical_channel_history 入参: channel_id={channel_id}, page={page}, page_size={page_size}, processed_type={processed_type}, processed_by={processed_by}")

    # 关联 PhysicalChannel 表，只查询未被废弃的物理频道的历史记录
    query = select(PhysicalChannelHistory).join(
        PhysicalChannel,
        PhysicalChannelHistory.physical_channel_id == PhysicalChannel.id,
        isouter=False
    ).where(
        PhysicalChannelHistory.channel_id == channel_id,
        PhysicalChannel.is_discarded.is_(False),
        PhysicalChannel.is_deleted.is_(False),
    )

    if processed_type:
        query = query.where(PhysicalChannelHistory.processed_type == processed_type)
    if processed_by:
        query = query.where(PhysicalChannelHistory.processed_by.ilike(f"%{processed_by}%"))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.order_by(PhysicalChannelHistory.id.desc()).offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    items = [
        PhysicalChannelHistoryItem(
            id=h.id,
            processed_at=h.processed_at,
            processed_by=h.processed_by,
            processed_type=h.processed_type,
            mediaservice=h.mediaservice,
            definition=h.definition,
            videoencode=h.videoencode,
            bitrate=h.bitrate,
        )
        for h in rows
    ]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


# ─── 内容-服务包关联 ───────────────────────────────────────────────────────

async def list_content_packages(db: AsyncSession, content_id: int) -> list[ContentPackageRef]:
    """
    查询内容关联的服务包列表。

    输入：content_id
    输出：list[ContentPackageRef]
    """
    logger.info(f"list_content_packages 入参: content_id={content_id}")
    links = (
        await db.execute(
            select(ContentPackage).where(
                ContentPackage.content_id == content_id,
                ContentPackage.is_deleted.is_(False),
                ContentPackage.is_discarded.is_(False),
            )
        )
    ).scalars().all()

    items = []
    for link in links:
        pkg = (
            await db.execute(select(Package).where(Package.id == link.package_id))
        ).scalar_one_or_none()
        if pkg and not pkg.is_deleted:
            items.append(ContentPackageRef(
                id=pkg.id,
                name=pkg.name,
                package_type=pkg.package_type,
                allocated_at=link.allocated_at,
            ))
    return items


async def link_content_packages(db: AsyncSession, content_id: int, package_ids: list[int], processed_by: str | None = None) -> list[ContentPackageRef]:
    """
    为内容关联服务包（批量）。

    输入：content_id, package_ids
    输出：list[ContentPackageRef]（新增的关联）
    """
    logger.info(f"link_content_packages 入参: content_id={content_id}, package_ids={package_ids}")
    # 查询已存在的关联
    existing = (
        await db.execute(
            select(ContentPackage.package_id).where(ContentPackage.content_id == content_id)
        )
    ).scalars().all()

    added = []
    for pid in package_ids:
        if pid in existing:
            continue
        # 校验 package 存在
        pkg = (
            await db.execute(
                select(Package).where(Package.id == pid, Package.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if not pkg:
            continue
        link = ContentPackage(content_id=content_id, package_id=pid)
        db.add(link)
        added.append(ContentPackageRef(
            id=pkg.id,
            name=pkg.name,
            package_type=pkg.package_type,
            allocated_at=None,  # 创建后 refresh
        ))

    await db.commit()
    # 重新查询获取 allocated_at
    result = await list_content_packages(db, content_id)

    # 完成流程节点并更新内容状态
    if added:
        content = (
            await db.execute(select(Content).where(Content.id == content_id))
        ).scalar_one_or_none()
        if content:
            await complete_process_and_update_status(
                db,
                content_id=content_id,
                content_type=content.content_type,
                process_name="Package",
                processed_by=processed_by,
                info=f"关联服务包: {', '.join(p.name for p in added)}",
            )
            await db.commit()

    return result


async def unlink_content_package(db: AsyncSession, content_id: int, package_id: int) -> None:
    """
    解除内容与服务包的关联。

    输入：content_id, package_id
    """
    logger.info(f"unlink_content_package 入参: content_id={content_id}, package_id={package_id}")
    link = (
        await db.execute(
            select(ContentPackage).where(
                ContentPackage.content_id == content_id,
                ContentPackage.package_id == package_id,
            )
        )
    ).scalar_one_or_none()
    if not link:
        raise NotFoundException(ErrorCode.CHANNEL_LINK_NOT_FOUND, get_msg("CHANNEL_LINK_NOT_FOUND"))
    await db.delete(link)
    await db.commit()


# ─── 内容-栏目关联 ───────────────────────────────────────────────────────────

async def list_content_categories(db: AsyncSession, content_id: int) -> list[ContentCategoryRef]:
    """
    查询内容关联的栏目列表。

    输入：content_id
    输出：list[ContentCategoryRef]
    """
    logger.info(f"list_content_categories 入参: content_id={content_id}")
    links = (
        await db.execute(
            select(ContentCategory).where(
                ContentCategory.content_id == content_id,
                ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False)
            )
        )
    ).scalars().all()

    items = []
    for link in links:
        cat = (
            await db.execute(
                select(Category).where(Category.id == link.category_id, Category.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if cat:
            # 查询父级栏目名称
            parent_name = None
            if cat.parent_id:
                parent = (
                    await db.execute(
                        select(Category.name).where(Category.id == cat.parent_id, Category.is_deleted.is_(False))
                    )
                ).scalar_one_or_none()
                parent_name = parent
            items.append(ContentCategoryRef(
                id=cat.id,
                name=cat.name,
                platform=cat.platform,
                parent_name=parent_name,
                allocated_at=link.allocated_at,
            ))
    return items


async def link_content_categories(db: AsyncSession, content_id: int, category_ids: list[int], processed_by: str | None = None) -> list[ContentCategoryRef]:
    """
    为内容关联栏目（批量）。

    输入：content_id, category_ids
    输出：list[ContentCategoryRef]（新增的关联）
    """
    logger.info(f"link_content_categories 入参: content_id={content_id}, category_ids={category_ids}")
    # 查询已存在的关联
    existing = (
        await db.execute(
            select(ContentCategory.category_id).where(
                ContentCategory.content_id == content_id,
                ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False)
            )
        )
    ).scalars().all()

    added = []
    for cid in category_ids:
        if cid in existing:
            continue
        # 校验 category 存在
        cat = (
            await db.execute(select(Category).where(Category.id == cid, Category.is_deleted.is_(False)))
        ).scalar_one_or_none()
        if not cat:
            continue
        
        # 校验 vod_count 限制
        if cat.vod_count > 0:
            current_count = (
                await db.execute(
                    select(func.count()).where(
                        ContentCategory.category_id == cid,
                        ContentCategory.is_deleted.is_(False),
                        ContentCategory.is_discarded.is_(False)
                    )
                )
            ).scalar_one()
            if current_count >= cat.vod_count:
                from app.common.core.exceptions import BusinessException
                from app.common.core.i18n import get_msg
                raise BusinessException(
                    ErrorCode.CATEGORY_CONTENT_LIMIT_EXCEEDED,
                    get_msg("CATEGORY_CONTENT_LIMIT_EXCEEDED", name=cat.name, limit=cat.vod_count)
                )
        
        link = ContentCategory(content_id=content_id, category_id=cid)
        db.add(link)
        added.append(ContentCategoryRef(
            id=cat.id,
            name=cat.name,
            platform=cat.platform,
            parent_name=None,
            allocated_at=None,
        ))

    await db.commit()
    result = await list_content_categories(db, content_id)

    # 完成流程节点并更新内容状态
    if added:
        content = (
            await db.execute(select(Content).where(Content.id == content_id))
        ).scalar_one_or_none()
        if content:
            await complete_process_and_update_status(
                db,
                content_id=content_id,
                content_type=content.content_type,
                process_name="Category",
                processed_by=processed_by,
                info=f"关联栏目: {', '.join(c.name for c in added)}",
            )
            await db.commit()

    return result


async def unlink_content_category(db: AsyncSession, content_id: int, category_id: int) -> None:
    """
    解除内容与栏目的关联。

    输入：content_id, category_id
    """
    logger.info(f"unlink_content_category 入参: content_id={content_id}, category_id={category_id}")
    link = (
        await db.execute(
            select(ContentCategory).where(
                ContentCategory.content_id == content_id,
                ContentCategory.category_id == category_id,
                ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not link:
        raise NotFoundException(ErrorCode.CHANNEL_LINK_NOT_FOUND, get_msg("CHANNEL_LINK_NOT_FOUND"))
    await db.delete(link)
    await db.commit()


# ─── 流程/日志查询 ───────────────────────────────────────────────────────────

async def list_processes(db: AsyncSession, content_id: int) -> list[ProcessListItem]:
    """
    查询内容的流程列表。

    输入：content_id
    输出：list[ProcessListItem]
    """
    from app.internal.cms_biz_orchestration.services import workflow_service
    logger.info(f"list_processes 入参: content_id={content_id}")
    content = (await db.execute(select(Content).where(Content.id == content_id))).scalar_one_or_none()
    current_status = content.status if content else None
    return await workflow_service.list_processes(db, content_id, current_status)


async def list_status_logs(db: AsyncSession, content_id: int) -> list[StatusLogListItem]:
    """
    查询内容的状态变更日志。

    输入：content_id
    输出：list[StatusLogListItem]
    """
    from app.internal.cms_biz_orchestration.services import workflow_service
    logger.info(f"list_status_logs 入参: content_id={content_id}")
    return await workflow_service.list_status_logs(db, content_id)


async def list_activity_logs(db: AsyncSession, content_id: int) -> list[ActivityLogListItem]:
    """
    查询内容的活动日志（从 operation_log 精确查询）。

    输入：content_id
    输出：list[ActivityLogListItem]
    """
    logger.info(f"list_activity_logs 入参: content_id={content_id}")
    from app.internal.cms_biz_system.models.operation_log import OperationLog

    logs = (
        await db.execute(
            select(OperationLog).where(
                OperationLog.content_id == content_id
            ).order_by(OperationLog.id.desc()).limit(50)
        )
    ).scalars().all()

    items = [
        ActivityLogListItem(
            id=log.id,
            processed_at=log.operation_time,
            processed_by=log.user_name,
            processed_type=log.operation_type,
            details=log.operation_content,
            previous_value=log.previous_value,
            updated_value=log.updated_value,
            updated_value_json=log.updated_value_json,
            entity_type=log.entity_type,
        )
        for log in logs
    ]
    return items


# ─── 审核管理 ───────────────────────────────────────────────────────────

async def _get_content_provider_review_info(db: AsyncSession, content_id: int) -> dict:
    """
    获取内容关联供应商的审批信息。
    
    返回：
        {
            "review_level": "None"/"L1"/"L2"/"L3",
            "level_required": 0/1/2/3,
            "l1_assignee_id": int|None,
            "l2_assignee_id": int|None,
            "l3_assignee_id": int|None,
        }
    """
    # 查询内容关联的许可证，获取供应商信息
    license_content = (
        await db.execute(
            select(LicenseContent)
            .join(License, LicenseContent.license_id == License.id)
            .join(Contract, License.contract_id == Contract.id)
            .join(Provider, Contract.provider_id == Provider.id)
            .where(
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    
    if not license_content:
        # 内容没有关联许可证，默认免审批
        return {
            "review_level": "None",
            "level_required": 0,
            "l1_assignee_id": None,
            "l2_assignee_id": None,
            "l3_assignee_id": None,
        }
    
    # 查询供应商的审批层级
    provider = (
        await db.execute(
            select(Provider)
            .join(Contract, Provider.id == Contract.provider_id)
            .join(License, Contract.id == License.contract_id)
            .join(LicenseContent, License.id == LicenseContent.license_id)
            .where(
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
                Provider.is_deleted.is_(False),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    
    if not provider or not provider.review_level:
        return {
            "review_level": "None",
            "level_required": 0,
            "l1_assignee_id": None,
            "l2_assignee_id": None,
            "l3_assignee_id": None,
        }
    
    # 解析审批层级
    review_level = provider.review_level
    if review_level == "L1":
        level_required = 1
    elif review_level == "L2":
        level_required = 2
    elif review_level == "L3":
        level_required = 3
    else:
        level_required = 0
    
    return {
        "review_level": review_level,
        "level_required": level_required,
        "l1_assignee_id": provider.l1_assignee_id,
        "l2_assignee_id": provider.l2_assignee_id,
        "l3_assignee_id": provider.l3_assignee_id,
    }


async def _get_content_provider_review_level(db: AsyncSession, content_id: int) -> tuple[str, int]:
    """
    获取内容关联供应商的审批层级（兼容旧接口）。
    
    返回：
        (review_level, level_required)
        review_level: None/L1/L2/L3
        level_required: 0/1/2/3
    """
    info = await _get_content_provider_review_info(db, content_id)
    return (info["review_level"], info["level_required"])


async def _create_publish_task_after_approval(db: AsyncSession, content_id: int, content_title: str, content_type: str) -> None:
    """审核通过后自动创建占位发布任务（状态为 none，不阻塞用户手动设置计划）。"""
    from app.internal.cms_biz_publish.models.publish_task import PublishTask
    
    # 检查是否已存在发布任务
    existing_task = (
        await db.execute(
            select(PublishTask).where(
                PublishTask.entity_id == content_id,
                PublishTask.entity_type == "Content",
                PublishTask.is_deleted.is_(False)
            )
        )
    ).scalar_one_or_none()
    
    if existing_task:
        logger.info(f"内容已存在发布任务，跳过自动创建 | content_id={content_id}")
        return
    
    # 创建发布任务
    publish_task = PublishTask(
        entity_type="Content",
        entity_id=content_id,
        entity_name=content_title,
        content_type=content_type,
        task_type="publish",
        execution_mode="plan",  # 默认为计划发布，用户可在发布管理列表中修改
        status="pending",
        publish_status="none",
    )
    
    db.add(publish_task)
    logger.info(f"自动创建发布任务 | content_id={content_id} title={content_title}")


async def initiate_content_review(
    db: AsyncSession,
    content_id: int,
    initiated_by: str,
) -> dict:
    """
    发起内容审核（内容编辑点击"发起审核"按钮）。
    
    根据供应商的审批层级决定流程：
    - 免审批：直接通过，自动创建发布任务
    - L1/L2/L3：创建审批记录，等待审批人审批
    
    输入：
        content_id: 内容ID
        initiated_by: 发起人用户名
        
    输出：
        dict: {success, content_id, review_status, message, auto_approved}
    """
    logger.info(f"发起内容审核 | content_id={content_id} initiated_by={initiated_by}")
    
    # 查询内容
    content = (
        await db.execute(
            select(Content).where(Content.id == content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
        )
    ).scalar_one_or_none()
    
    if not content:
        raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))

    # ─── 校验许可证绑定 ──────────────────────────────────────────────────────
    license_content = (
        await db.execute(
            select(LicenseContent)
            .join(License, LicenseContent.license_id == License.id)
            .join(Contract, License.contract_id == Contract.id)
            .join(Provider, Contract.provider_id == Provider.id)
            .where(
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
            )
            .limit(1)
        )
    ).scalar_one_or_none()

    if not license_content:
        raise BusinessException(ErrorCode.CONTENT_NO_LICENSE_BOUND, get_msg("CONTENT_NO_LICENSE_BOUND"))

    # ─── 前置条件校验（暂时注释掉，以前端页面状态为准）───────────────────────
    # 使用统一的状态检查服务，确保前后端判断逻辑一致
    # missing_items = []
    #
    # # 1. 校验材料注入（Movies）
    # if not await ContentStatusService.check_materials(db, content_id):
    #     missing_items.append("材料注入")
    #
    # # 2. 校验元数据（Metadata）
    # if not await ContentStatusService.check_metadata(db, content_id):
    #     missing_items.append("元数据")
    #
    # # 3. 校验海报（Pictures）
    # if not await ContentStatusService.check_posters(db, content_id, content.content_type):
    #     missing_items.append("海报")
    #
    # # 4. 校验演员角色映射（CastRoleMap）
    # if not await ContentStatusService.check_cast_role_map(db, content_id):
    #     missing_items.append("演员角色映射")
    #
    # # 5. 校验服务包（Package）
    # if not await ContentStatusService.check_package(db, content_id):
    #     missing_items.append("服务包")
    #
    # # 6. 校验栏目（Category）
    # if not await ContentStatusService.check_category(db, content_id):
    #     missing_items.append("栏目")
    #
    # # 如果有未完成项，返回错误
    # if missing_items:
    #     error_msg = f"以下项目未完成，无法发起审核：{', '.join(missing_items)}"
    #     logger.warning(f"发起审核前置校验失败 | content_id={content_id} missing={missing_items}")
    #     raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=error_msg)
    #
    # logger.info(f"发起审核前置校验通过 | content_id={content_id}")
    
    # 获取供应商审批信息
    review_info = await _get_content_provider_review_info(db, content_id)
    review_level = review_info["review_level"]
    level_required = review_info["level_required"]
    l1_assignee_id = review_info["l1_assignee_id"]
    l2_assignee_id = review_info["l2_assignee_id"]
    l3_assignee_id = review_info["l3_assignee_id"]
    logger.info(f"供应商审批层级 | content_id={content_id} review_level={review_level} level_required={level_required}")
    
    # 导入模型
    from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
    from app.internal.cms_biz_orchestration.models.content_review import ContentReview
    from app.internal.cms_biz_package.models.task import Task
    from app.internal.cms_biz_system.models.user import User
    
    now = datetime.now()
    
    # 创建或更新"申请审核"流程节点（标记为已完成，表示已发起申请）
    application_review = (
        await db.execute(
            select(ContentProcess).where(
                ContentProcess.content_id == content_id,
                ContentProcess.node_code == "ApplicationReview",
                ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    
    if not application_review:
        application_review = ContentProcess(
            content_id=content_id,
            name="ApplicationReview",
            node_code="ApplicationReview",
            sequence=3,
            start_dt=now,
            end_dt=now,
            status="Passed",
            assigned=initiated_by,  # 记录发起人/处理人
            info=f"申请已发起: {initiated_by}",
        )
        db.add(application_review)
    else:
        application_review.start_dt = now
        application_review.end_dt = now
        application_review.status = "Passed"
        application_review.assigned = initiated_by  # 更新处理人
        application_review.info = f"申请已发起: {initiated_by}"
    
    # 免审批：直接通过
    if level_required == 0:
        # 查询或创建"内容审核"流程节点
        review_process = (
            await db.execute(
                select(ContentProcess).where(
                    ContentProcess.content_id == content_id,
                    ContentProcess.node_code == "ContentReview",
                    ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
                )
            )
        ).scalar_one_or_none()
        
        if not review_process:
            review_process = ContentProcess(
                content_id=content_id,
                name="ContentReview",
                node_code="ContentReview",
                sequence=4,
                start_dt=now,
                status="Passed",
                end_dt=now,
                info="免审批，自动通过",
            )
            db.add(review_process)
        else:
            review_process.status = "Passed"
            review_process.end_dt = now
            review_process.info = "免审批，自动通过"
        
        # 更新内容状态为 ReadyForPublish
        old_status = content.status
        content.status = "ReadyForPublish"
        
        # 记录状态变更日志
        from app.internal.cms_biz_orchestration.services.workflow_service import record_status_change
        await record_status_change(db, content_id, old_status, "ReadyForPublish", initiated_by)
        
        # 自动创建发布任务
        await _create_publish_task_after_approval(db, content_id, content.title, content.content_type)
        
        logger.info(f"免审批，自动通过并创建发布任务 | content_id={content_id}")
        
        return {
            "success": True,
            "content_id": content_id,
            "review_status": "approved",
            "message": "免审批，自动通过",
            "auto_approved": True,
        }
    
    # 查询是否已有审批记录
    existing_review = (
        await db.execute(
            select(ContentReview)
            .where(
                ContentReview.content_id == content_id,
                ContentReview.is_deleted.is_(False),
            )
            .order_by(ContentReview.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    if existing_review:
        if existing_review.final_status == "Pending":
            # 审批进行中，不允许重复提交
            raise BusinessException(ErrorCode.REVIEW_ALREADY_IN_PROGRESS, get_msg("REVIEW_ALREADY_IN_PROGRESS"))
        elif existing_review.final_status == "Rejected":
            # 之前被拒绝，软删除旧的审批记录和任务
            existing_review.is_deleted = True
            logger.info(f"重新发起审核，软删除旧审批记录 | content_id={content_id} review_id={existing_review.id}")

            # 软删除旧的审批任务
            existing_review_tasks = (
                await db.execute(
                    select(Task).where(
                        Task.content_id == content_id,
                        Task.task_type.in_(["review L1", "review L2", "review L3"]),
                        Task.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
            for task in existing_review_tasks:
                task.is_deleted = True
                logger.info(f"重新发起审核，软删除旧审批任务 | content_id={content_id} task_id={task.id} task_type={task.task_type}")

            # 软删除旧的 ContentReview 流程记录
            existing_processes = (
                await db.execute(
                    select(ContentProcess).where(
                        ContentProcess.content_id == content_id,
                        ContentProcess.node_code == "ContentReview",
                        ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for process in existing_processes:
                process.is_deleted = True
                logger.info(f"重新发起审核，软删除旧流程记录 | content_id={content_id} process_id={process.id}")
        # 如果是 Passed 状态，允许创建新的审批记录（重新审核流程）

    # 创建新的审批记录
    content_review = ContentReview(
        content_id=content_id,
        review_level=review_level,
        level_required=level_required,
        final_status="Pending",
        initiated_by=initiated_by,
        initiated_at=now,
    )
    db.add(content_review)

    # 更新 arrangement 任务状态为已完成（内容编排人员申请审批后，arrangement任务完成）
    arrangement_task = (
        await db.execute(
            select(Task).where(
                Task.content_id == content_id,
                Task.task_type == "arrangement",
                Task.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if arrangement_task:
        arrangement_task.task_status = "Completed"
        arrangement_task.end_time = now
        logger.info(f"arrangement任务完成 | content_id={content_id} task_id={arrangement_task.id}")
    
    # 创建 L1 审核任务（从供应商获取默认审批人）
    l1_task = Task(
        content_id=content_id,
        task_type="review L1",
        assignee_id=l1_assignee_id,  # 从供应商配置获取默认审批人
        task_status="Pending" if l1_assignee_id else "Not Assigned",
        start_time=now,
        end_time=None,
    )
    db.add(l1_task)
    
    # 为 L1 审批人添加数据权限
    if l1_assignee_id:
        await _ensure_content_auth(db, content_id, l1_assignee_id)
    
    # 查询 L1 审批人用户名
    l1_assignee_name = None
    if l1_assignee_id:
        l1_user = (await db.execute(select(User).where(User.id == l1_assignee_id, User.is_deleted.is_(False)))).scalar_one_or_none()
        l1_assignee_name = l1_user.username if l1_user else None
    
    # 创建 L1 流程记录
    l1_process = ContentProcess(
        content_id=content_id,
        name="ContentReview",
        node_code="ContentReview",
        sequence=4,
        start_dt=now,
        status="Pending",
        assigned=l1_assignee_name,
        info=f"L1审批: {l1_assignee_name or '未分配'}",
    )
    db.add(l1_process)
    
    # L2/L3 任务在上一级审批通过后创建，这里不创建
    logger.info(f"创建L1审批任务，等待审批 | content_id={content_id} level_required={level_required}")
    
    return {
        "success": True,
        "content_id": content_id,
        "review_status": "pending",
        "message": f"已发起审核，需要{level_required}级审批",
        "auto_approved": False,
        "level_required": level_required,
    }


async def submit_content_review(
    db: AsyncSession,
    content_id: int,
    review_type: str,
    issue_types: Optional[list[str]],
    description: Optional[str],
    processed_by: str,
    review_level: Optional[str] = None,  # L1/L2/L3，审批人级别
) -> dict:
    """
    提交内容审核（审批人操作）。

    输入：
        content_id: 内容ID
        review_type: 审核类型（approve/reject）
        issue_types: 内容问题类型列表（审核不通过时）
        description: 审核说明
        processed_by: 处理人（审批人用户名）
        review_level: 审批级别（L1/L2/L3），用于多级审批

    输出：
        dict: {success, content_id, review_status, message, final_approved}
    """
    logger.info(f"提交内容审核 | content_id={content_id} review_type={review_type} review_level={review_level}")

    # 查询内容
    content = (
        await db.execute(
            select(Content).where(Content.id == content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
        )
    ).scalar_one_or_none()

    if not content:
        raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))

    # 导入模型
    from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
    from app.internal.cms_biz_orchestration.models.content_review import ContentReview
    from app.internal.cms_biz_package.models.task import Task
    from app.internal.cms_biz_system.models.user import User
    
    # 查询最新的审批记录
    content_review = (
        await db.execute(
            select(ContentReview)
            .where(
                ContentReview.content_id == content_id,
                ContentReview.is_deleted.is_(False),
            )
            .order_by(ContentReview.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    
    if not content_review:
        raise BusinessException(ErrorCode.REVIEW_RECORD_NOT_FOUND, get_msg("REVIEW_RECORD_NOT_FOUND"))
    
    if content_review.final_status != "Pending":
        raise BusinessException(ErrorCode.REVIEW_ALREADY_COMPLETED, get_msg("REVIEW_ALREADY_COMPLETED"))
    
    now = datetime.now()
    
    # 获取审批配置
    level_required = content_review.level_required
    
    # 自动推断当前应该审批的级别
    # 根据 content_review 中各级别的状态来确定当前级别
    if content_review.level_1_status == "Pending":
        current_level = "L1"
    elif content_review.level_2_status == "Pending" and level_required >= 2:
        current_level = "L2"
    elif content_review.level_3_status == "Pending" and level_required >= 3:
        current_level = "L3"
    else:
        # 如果没有Pending的状态，使用前端传入的参数或默认为L1
        current_level = review_level or "L1"
    
    logger.info(f"自动推断审批级别 | content_id={content_id} current_level={current_level} L1={content_review.level_1_status} L2={content_review.level_2_status} L3={content_review.level_3_status}")
    
    level_num = int(current_level[1]) if current_level.startswith("L") else 1
    
    # 更新对应级别的审批状态
    if level_num >= 1:
        content_review.level_1_status = "Passed" if review_type == "approve" else "Rejected"
        content_review.level_1_by = processed_by
        content_review.level_1_at = now
        content_review.level_1_comment = description
    
    if level_num >= 2 and review_type == "approve":
        content_review.level_2_status = "Passed" if review_type == "approve" else "Rejected"
        content_review.level_2_by = processed_by
        content_review.level_2_at = now
        content_review.level_2_comment = description
    
    if level_num >= 3 and review_type == "approve":
        content_review.level_3_status = "Passed" if review_type == "approve" else "Rejected"
        content_review.level_3_by = processed_by
        content_review.level_3_at = now
        content_review.level_3_comment = description
    
    # 判断是否完成所有审批
    all_approved = (
        (level_required < 1 or content_review.level_1_status == "Passed") and
        (level_required < 2 or content_review.level_2_status == "Passed") and
        (level_required < 3 or content_review.level_3_status == "Passed")
    )
    
    any_rejected = (
        content_review.level_1_status == "Rejected" or
        content_review.level_2_status == "Rejected" or
        content_review.level_3_status == "Rejected"
    )
    
    # ADMIN 角色用户跳过任务查询和权限校验
    is_admin = await is_admin_user(db, processed_by)
    if is_admin:
        logger.info(f"ADMIN 用户跳过审批任务查询和权限校验 | username={processed_by}")
        current_task = None
    else:
        # 查询当前级别的任务（只查询Pending状态的任务，确保唯一性）
        current_task = (
            await db.execute(
                select(Task).where(
                    Task.content_id == content_id,
                    Task.task_type == f"review {current_level}",
                    Task.task_status == "Pending",
                    Task.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()

        if not current_task:
            raise NotFoundException(ErrorCode.REVIEW_TASK_NOT_FOUND, get_msg("REVIEW_TASK_NOT_FOUND"))

        # 校验当前用户是否为任务分配人（审批权限校验）
        if current_task.assignee_id:
            current_user = (
                await db.execute(
                    select(User).where(
                        User.username == processed_by,
                        User.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()

            if not current_user or current_user.id != current_task.assignee_id:
                # 查询任务的分配人信息
                assignee_user = (
                    await db.execute(
                        select(User).where(
                            User.id == current_task.assignee_id,
                            User.is_deleted.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                assignee_name = assignee_user.username if assignee_user else "未知"
                raise ForbiddenException(ErrorCode.REVIEW_NO_PERMISSION, get_msg("REVIEW_NO_PERMISSION"))

    # 查询当前级别的流程记录（最新的Pending状态）
    current_process = (
        await db.execute(
            select(ContentProcess).where(
                ContentProcess.content_id == content_id,
                ContentProcess.node_code == "ContentReview",
                ContentProcess.status == "Pending",
                ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
            ).order_by(ContentProcess.created_at.desc())
        )
    ).scalar_one_or_none()
    
    if review_type == "approve" and all_approved:
        # 全部审批通过
        content_review.final_status = "Passed"
        content_review.completed_at = now
        
        # 完成当前任务
        if current_task:
            current_task.task_status = "Completed"
            current_task.end_time = now
            
            # 记录任务历史
            from app.internal.cms_biz_package.models.task import TaskHistory
            db.add(
                TaskHistory(
                    task_id=current_task.id,
                    processed_type="Review",
                    processed_by=processed_by,
                    previous_value="Pending",
                    updated_value=f"审批通过: {description or ''}",
                )
            )
        
        # 完成当前流程记录
        if current_process:
            current_process.status = "Passed"
            current_process.end_dt = now
            current_process.assigned = processed_by
            current_process.info = f"{current_level}审批通过: {processed_by}"
        
        # 更新内容状态（不创建新的流程记录，因为上面已经更新了）
        from app.internal.cms_biz_orchestration.services.workflow_service import (
            update_content_status_by_process_completion,
            _update_status_by_default_rules,
        )
        new_status = await update_content_status_by_process_completion(
            db, content_id, content.content_type, "ContentReview", processed_by
        )
        if new_status is None:
            new_status = await _update_status_by_default_rules(
                db, content_id, content.content_type, "ContentReview", processed_by
            )
        # 同步父内容状态
        if new_status is not None:
            from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
            await ContentStatusService.sync_parent_status(db, content_id, content.content_type)
        
        # 自动创建发布任务
        await _create_publish_task_after_approval(db, content_id, content.title, content.content_type)
        
        logger.info(f"内容审核全部通过 | content_id={content_id} by={processed_by}")
        
        return {
            "success": True,
            "content_id": content_id,
            "review_status": "approved",
            "message": "审核通过",
            "final_approved": True,
        }
        
    elif review_type == "reject" or any_rejected:
        # 任一审批不通过
        content_review.final_status = "Rejected"
        content_review.completed_at = now
        
        # 拒绝当前任务
        if current_task:
            current_task.task_status = "Rejected"
            current_task.end_time = now
            
            # 记录任务历史
            from app.internal.cms_biz_package.models.task import TaskHistory
            db.add(
                TaskHistory(
                    task_id=current_task.id,
                    processed_type="Review",
                    processed_by=processed_by,
                    previous_value="Pending",
                    updated_value=f"审批拒绝: {description or ''}",
                )
            )
        
        # 当前流程记录标记为失败
        if current_process:
            current_process.status = "Failed"
            current_process.end_dt = now
            current_process.assigned = processed_by
            current_process.info = f"{current_level}审批不通过: {processed_by} - {description or ''}"
        
        # 更新内容状态（不创建新的流程记录，因为上面已经更新了）
        from app.internal.cms_biz_orchestration.services.workflow_service import (
            update_content_status_by_process_completion,
            _update_status_by_default_rules,
        )
        new_status = await update_content_status_by_process_completion(
            db, content_id, content.content_type, "ContentReview", processed_by
        )
        if new_status is None:
            new_status = await _update_status_by_default_rules(
                db, content_id, content.content_type, "ContentReview", processed_by
            )
        # 同步父内容状态
        if new_status is not None:
            from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
            await ContentStatusService.sync_parent_status(db, content_id, content.content_type)
        
        # 审核不通过，恢复 arrangement 任务为待处理状态
        arrangement_task = (
            await db.execute(
                select(Task).where(
                    Task.content_id == content_id,
                    Task.task_type == "arrangement",
                    Task.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if arrangement_task:
            arrangement_task.task_status = "Pending"
            arrangement_task.end_time = None
            logger.info(f"审核不通过，arrangement任务恢复为待处理 | content_id={content_id}")
        
        logger.info(f"内容审核不通过 | content_id={content_id} by={processed_by} issues={issue_types}")
        
        return {
            "success": True,
            "content_id": content_id,
            "review_status": "rejected",
            "message": "审核不通过",
            "final_approved": False,
        }
    
    else:
        # 部分通过，等待下一级审批
        # 完成当前任务
        if current_task:
            current_task.task_status = "Completed"
            current_task.end_time = now
            
            # 记录任务历史
            from app.internal.cms_biz_package.models.task import TaskHistory
            db.add(
                TaskHistory(
                    task_id=current_task.id,
                    processed_type="Review",
                    processed_by=processed_by,
                    previous_value="Pending",
                    updated_value=f"{current_level}审批通过，等待下一级审批: {description or ''}",
                )
            )
        
        # 完成当前流程记录（使用"Finished"而非"Passed"，表示本级审批完成但整个ContentReview未结束）
        if current_process:
            current_process.status = "Finished"
            current_process.end_dt = now
            current_process.assigned = processed_by
            current_process.info = get_msg("REVIEW_LEVEL_APPROVED_WAITING_NEXT", level=current_level, by=processed_by)
        
        # 创建下一级任务并创建流程记录
        next_level = None
        if current_level == "L1" and level_required >= 2:
            next_level = "L2"
        elif current_level == "L2" and level_required >= 3:
            next_level = "L3"
        
        if next_level:
            # 获取供应商配置的下一级审批人
            review_info = await _get_content_provider_review_info(db, content_id)
            next_assignee_id = review_info.get(f"{next_level.lower()}_assignee_id")
            
            # 创建下一级任务
            next_task = Task(
                content_id=content_id,
                task_type=f"review {next_level}",
                assignee_id=next_assignee_id,
                task_status="Pending" if next_assignee_id else "Not Assigned",
                start_time=now,
                end_time=None,
            )
            db.add(next_task)
            await db.flush()
            
            # 为下一级审批人添加数据权限
            if next_assignee_id:
                await _ensure_content_auth(db, content_id, next_assignee_id)
            
            # 查询下一级审批人名称
            next_assignee_name = None
            if next_assignee_id:
                next_user = (await db.execute(select(User).where(User.id == next_assignee_id, User.is_deleted.is_(False)))).scalar_one_or_none()
                next_assignee_name = next_user.username if next_user else None
            
            # 创建下一级流程记录
            next_process = ContentProcess(
                content_id=content_id,
                name="ContentReview",
                node_code="ContentReview",
                sequence=4,
                start_dt=now,
                status="Pending",
                assigned=next_assignee_name,
                info=f"{next_level}审批: {next_assignee_name or '未分配'}",
            )
            db.add(next_process)
            
            logger.info(f"{current_level}审批通过，创建{next_level}任务和流程记录 | content_id={content_id} assignee={next_assignee_name}")
        
        logger.info(f"内容审核部分通过，等待下一级 | content_id={content_id} current_level={current_level}")
        
        return {
            "success": True,
            "content_id": content_id,
            "review_status": "pending",
            "message": f"{current_level}审批通过，等待下一级审批",
            "final_approved": False,
        }


async def get_content_review_status(db: AsyncSession, content_id: int) -> Optional[dict]:
    """获取内容审核状态。"""
    from app.internal.cms_biz_orchestration.models.content_review import ContentReview
    
    content_review = (
        await db.execute(
            select(ContentReview)
            .where(
                ContentReview.content_id == content_id,
                ContentReview.is_deleted.is_(False),
            )
            .order_by(ContentReview.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    
    if not content_review:
        return None
    
    return {
        "id": content_review.id,
        "content_id": content_review.content_id,
        "review_level": content_review.review_level,
        "level_required": content_review.level_required,
        "level_1_status": content_review.level_1_status,
        "level_1_by": content_review.level_1_by,
        "level_1_at": content_review.level_1_at,
        "level_2_status": content_review.level_2_status,
        "level_2_by": content_review.level_2_by,
        "level_2_at": content_review.level_2_at,
        "level_3_status": content_review.level_3_status,
        "level_3_by": content_review.level_3_by,
        "level_3_at": content_review.level_3_at,
        "final_status": content_review.final_status,
        "initiated_by": content_review.initiated_by,
        "initiated_at": content_review.initiated_at,
        "completed_at": content_review.completed_at,
    }


async def check_content_review_permission(db: AsyncSession, content_id: int, username: str) -> dict:
    """
    校验当前用户是否有权限进行内容审核操作。
    
    根据审批记录自动推断当前应该审批的级别，并检查当前用户是否为该级别的任务指派人。
    
    返回：
        dict: {
            has_permission: bool,
            current_level: str (L1/L2/L3),
            assignee_name: str | null (当前级别指派人名称),
            message: str (无权限时的提示消息)
        }
    """
    from app.internal.cms_biz_orchestration.models.content_review import ContentReview
    from app.internal.cms_biz_package.models.task import Task
    from app.internal.cms_biz_system.models.user import User
    
    # ADMIN 角色直接放行
    if await is_admin_user(db, username):
        return {
            "has_permission": True,
            "current_level": None,
            "assignee_name": None,
            "message": None,
        }
    
    # 查询最新的审批记录
    content_review = (
        await db.execute(
            select(ContentReview)
            .where(
                ContentReview.content_id == content_id,
                ContentReview.is_deleted.is_(False),
            )
            .order_by(ContentReview.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    
    if not content_review or content_review.final_status != "Pending":
        return {
            "has_permission": False,
            "current_level": None,
            "assignee_name": None,
            "message": get_msg("REVIEW_RECORD_NOT_FOUND"),
        }
    
    level_required = content_review.level_required
    
    # 自动推断当前应该审批的级别
    if content_review.level_1_status == "Pending":
        current_level = "L1"
    elif content_review.level_2_status == "Pending" and level_required >= 2:
        current_level = "L2"
    elif content_review.level_3_status == "Pending" and level_required >= 3:
        current_level = "L3"
    else:
        current_level = "L1"
    
    # 查询当前级别的 Pending 任务
    current_task = (
        await db.execute(
            select(Task).where(
                Task.content_id == content_id,
                Task.task_type == f"review {current_level}",
                Task.task_status == "Pending",
                Task.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    
    if not current_task:
        return {
            "has_permission": False,
            "current_level": current_level,
            "assignee_name": None,
            "message": get_msg("REVIEW_TASK_NOT_FOUND"),
        }
    
    # 查询当前用户
    current_user = (
        await db.execute(
            select(User).where(
                User.username == username,
                User.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    
    if not current_user:
        return {
            "has_permission": False,
            "current_level": current_level,
            "assignee_name": None,
            "message": get_msg("USER_NOT_FOUND"),
        }
    
    # 查询指派人名称
    assignee_name = None
    if current_task.assignee_id:
        assignee_user = (
            await db.execute(
                select(User).where(
                    User.id == current_task.assignee_id,
                    User.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        assignee_name = assignee_user.username if assignee_user else None
    
    # 校验当前用户是否为任务指派人
    if current_task.assignee_id and current_user.id != current_task.assignee_id:
        return {
            "has_permission": False,
            "current_level": current_level,
            "assignee_name": assignee_name,
            "message": get_msg("REVIEW_NO_PERMISSION"),
        }
    
    return {
        "has_permission": True,
        "current_level": current_level,
        "assignee_name": assignee_name,
        "message": None,
    }
