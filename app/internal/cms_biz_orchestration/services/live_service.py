"""
直播管理（Live）业务逻辑层。

职责：
- 频道管理：查询 CHANNEL 类型内容列表（含服务包/供应商/许可证关联）；频道详情；频道更新
- 节目单管理：查询 SCHEDULE 类型内容列表（含所属频道名称）；新增/删除节目单
- 归档管理：查询 MOVIE/EPISODE/SEASON/SEASON_SERIES/SERIES 类型内容列表（同 VOD 视角，含关联信息）
- 物理频道管理：查询/新增/编辑/删除物理频道
- 内容-服务包关联：查询/新增/删除关联
- 内容-栏目关联：查询/新增/删除关联
- 流程/日志：查询流程、状态日志、活动日志

未实现字段（模型尚无对应字段，返回 None 占位）：
- cutv_enable（CUTV 启用）
- archived（是否归档）
- category_name（栏目）
- publish_date / unpublish_date（发布/下架日期）
"""

import io
import json
from datetime import date, datetime, timezone
from typing import Optional

from fastapi import UploadFile
from pydantic import BaseModel
from loguru import logger
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import String, and_, case, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import app_tz
from app.internal.cms_biz_metada.models.basic import Category, Genre, CustomField, EntityFieldValue, CustomTag, Tag, ContentType as ContentTypeModel
from app.internal.cms_biz_package.models import ContentType, ContentStatus
from app.internal.cms_biz_package.models.package import Content, ContentGenre, ContentPackage, ContentCategory, ContentCustomTag, Package, PhysicalChannel, PhysicalChannelHistory
from app.internal.cms_biz_orchestration.models.content_metadata import ContentMetadata, SeriesMetadata, ChannelMetadata, ScheduleMetadata
from app.internal.cms_biz_orchestration.schemas.content_metadata import normalize_sections_info
from app.internal.cms_biz_scp.models.trade import Contract, License, LicenseContent, Provider
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.models.content_auth import ContentAuth
from app.internal.cms_biz_system.services.data_auth_filter import apply_content_data_auth
from app.internal.cms_biz_system.services.operation_log_service import write_log, OperationType
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
from app.internal.cms_biz_system.services.dict_service import get_dict_children_by_code
from app.internal.cms_biz_orchestration.services.content_service import (
    _apply_meta_fields,
    _cell_str,
    _normalize_vod_header,
    _parse_date,
    _resolve_basic_data_strict,
    _resolve_relation_ids,
)
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
    rollback_after_published_edit,
    rollback_ancestors_after_child_change,
)
from app.internal.cms_biz_package.services.task_service import (
    _ensure_content_auth,
    create_arrangement_task,
)
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode, ForbiddenException
from app.common.utils import is_admin_user
from app.internal.cms_biz_orchestration.services.dict_utils import resolve_dict_codes_strict, resolve_single_dict_code_strict


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _get_genre_names(db: AsyncSession, content_id: int) -> tuple[Optional[list[int]], Optional[str]]:
    """查询题材 ID 列表和名称列表（从中间表 content_genre）。"""
    rows = (
        await db.execute(
            select(ContentGenre.genre_id, Genre.name)
            .join(Genre, ContentGenre.genre_id == Genre.id)
            .where(ContentGenre.content_id == content_id, Genre.is_deleted.is_(False))
        )
    ).all()
    if not rows:
        return None, None
    ids = [r.genre_id for r in rows]
    names = ", ".join(r.name for r in rows)
    return ids, names


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

async def _build_channel_item(db: AsyncSession, c: Content, meta_name: Optional[str] = None) -> ChannelListItem:
    """将 ORM Content（CHANNEL 类型）转换为频道列表响应。"""
    genre_ids, genre_name = await _get_genre_names(db, c.id)
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
        genre_ids=genre_ids,
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
        is_discarded=c.is_discarded,
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
    publish_date_from: Optional[str] = None,
    publish_date_to: Optional[str] = None,
    unpublish_date_from: Optional[str] = None,
    unpublish_date_to: Optional[str] = None,
    is_discarded: Optional[bool] = None,
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
        publish_date_from   发布日期范围下限（YYYY-MM-DD）
        publish_date_to     发布日期范围上限
        unpublish_date_from 下架日期范围下限（YYYY-MM-DD）
        unpublish_date_to   下架日期范围上限
        is_discarded        是否废弃（布尔值，默认 False）

    输出：
        PaginatedResponse[ChannelListItem]
    """
    logger.info(f"list_channels 入参: page={page}, page_size={page_size}, title={title}, statuses={statuses}, genre_ids={genre_ids}, provider_id={provider_id}, provider_ids={provider_ids}, package_name={package_name}, package_id={package_id}, package_ids={package_ids}, category_id={category_id}, category_name={category_name}, custom_tag_ids={custom_tag_ids}, channel_number={channel_number}, languages={languages}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, publish_date_from={publish_date_from}, publish_date_to={publish_date_to}, unpublish_date_from={unpublish_date_from}, unpublish_date_to={unpublish_date_to}, is_discarded={is_discarded}, sort_by={sort_by}, sort_order={sort_order}")
    
    query = select(Content).where(
        Content.content_type == ContentType.CHANNEL.value,
        Content.is_deleted.is_(False),
    )
    if is_discarded is not None:
        query = query.where(Content.is_discarded.is_(is_discarded))
    else:
        query = query.where(Content.is_discarded.is_(False))
    # 数据权限过滤：admin 不过滤；其他用户仅看自己创建的或被授权的 CHANNEL
    query = await apply_content_data_auth(db, current_user, query)

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if statuses:
        query = query.where(Content.status.in_(statuses))
    # 题材过滤：单选 genre_id（向下兼容）与多选 genre_ids 合并
    _genre_id_list = list(filter(None, ([genre_id] if genre_id else []) + (genre_ids or [])))
    if _genre_id_list:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id.in_(_genre_id_list))
        query = query.where(Content.id.in_(subq))

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

    # 许可证日期范围过滤 — Overlap 语义
    # 当开始日期和结束日期范围都提供时，查找许可证有效期与查询周期重叠的频道
    # 重叠条件：License.start_date <= 查询截止 AND License.end_date >= 查询起始
    if license_start_from or license_start_to or license_end_from or license_end_to:
        lic_query = select(License.id).where(License.is_deleted.is_(False))

        if license_start_from and license_end_to:
            # 两个范围都激活 → 使用 Overlap 语义（start_date <= end_to AND end_date >= start_from）
            query_start = date.fromisoformat(str(license_start_from))
            query_end = date.fromisoformat(str(license_end_to))
            lic_query = lic_query.where(
                License.start_date <= query_end,
                License.end_date >= query_start,
            )
        else:
            # 只有一个范围激活 → 保持原有的逐字段范围过滤
            if license_start_from:
                lic_query = lic_query.where(License.start_date >= date.fromisoformat(str(license_start_from)))
            if license_start_to:
                lic_query = lic_query.where(License.start_date <= date.fromisoformat(str(license_start_to)))
            if license_end_from:
                lic_query = lic_query.where(License.end_date >= date.fromisoformat(str(license_end_from)))
            if license_end_to:
                lic_query = lic_query.where(License.end_date <= date.fromisoformat(str(license_end_to)))

        content_ids_with_lic = select(LicenseContent.content_id).where(
            LicenseContent.license_id.in_(lic_query),
            LicenseContent.is_deleted.is_(False),
        )
        query = query.where(Content.id.in_(content_ids_with_lic))

    # 发布日期范围过滤：通过 PublishTask.publish_time 查询
    if publish_date_from or publish_date_to:
        pub_query = select(PublishTask.entity_id).where(
            PublishTask.entity_type == 'Content',
            PublishTask.task_type == 'publish',
            PublishTask.status == 'success',
            PublishTask.is_deleted.is_(False),
        )
        if publish_date_from:
            publish_date_from_str = str(publish_date_from) if not isinstance(publish_date_from, str) else publish_date_from
            pub_query = pub_query.where(PublishTask.publish_time >= datetime.fromisoformat(publish_date_from_str).replace(tzinfo=None))
        if publish_date_to:
            publish_date_to_str = str(publish_date_to) if not isinstance(publish_date_to, str) else publish_date_to
            pub_query = pub_query.where(PublishTask.publish_time <= datetime.fromisoformat(publish_date_to_str).replace(hour=23, minute=59, second=59, tzinfo=None))
        query = query.where(Content.id.in_(pub_query))

    # 下架日期范围过滤：通过 PublishTask.unpublish_time 查询
    # 注意:CHANNEL/SCHEDULE 的下架时间记录在 task_type=publish 的记录中,而非独立的 unpublish 记录
    if unpublish_date_from or unpublish_date_to:
        unpub_query = select(PublishTask.entity_id).where(
            PublishTask.entity_type == 'Content',
            PublishTask.unpublish_time.is_not(None),
            PublishTask.is_deleted.is_(False),
        )
        if unpublish_date_from:
            date_from_str = str(unpublish_date_from) if not isinstance(unpublish_date_from, str) else unpublish_date_from
            unpub_query = unpub_query.where(PublishTask.unpublish_time >= datetime.fromisoformat(date_from_str).replace(tzinfo=None))
        if unpublish_date_to:
            date_to_str = str(unpublish_date_to) if not isinstance(unpublish_date_to, str) else unpublish_date_to
            unpub_query = unpub_query.where(PublishTask.unpublish_time <= datetime.fromisoformat(date_to_str).replace(hour=23, minute=59, second=59, tzinfo=None))
        query = query.where(Content.id.in_(unpub_query))

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

    # 批量查询元数据名称
    content_ids = [c.id for c in rows]
    meta_rows = (
        await db.execute(
            select(ChannelMetadata.content_id, ChannelMetadata.name)
            .where(
                ChannelMetadata.content_id.in_(content_ids),
                ChannelMetadata.is_deleted.is_(False),
                ChannelMetadata.is_discarded.is_(False),
            )
        )
    ).all()
    meta_map = {row.content_id: row.name for row in meta_rows}

    items = [await _build_channel_item(db, c, meta_map.get(c.id)) for c in rows]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


# ─── 节目单管理（SCHEDULE）────────────────────────────────────────────

async def build_schedule_item(db: AsyncSession, c: Content, meta_name: Optional[str] = None) -> ScheduleListItem:
    """将 ORM Content（SCHEDULE 类型）转换为节目单列表响应。"""
    # 查询频道名称（parent Content.title）
    channel_name_val: Optional[str] = None
    if c.parent_id:
        channel_name_val = (
            await db.execute(
                select(Content.title).where(Content.id == c.parent_id, Content.is_deleted.is_(False))
            )
        ).scalar_one_or_none()

    # 查询归档产物（source_schedule_id == c.id 的 Content）
    archive_content_id: Optional[int] = None
    archive_content_type: Optional[str] = None
    archive_published: bool = False

    # 查询节目单本身的发布状态
    from app.internal.cms_biz_publish.repositories import publish_repository
    publish_status = await publish_repository.get_object_publish_status(
        db, "Content", c.id
    )
    is_published = publish_status.is_published if publish_status else False

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
        channel_name=channel_name_val,
        begin_time=c.begin_time,
        end_time=c.end_time,
        cutv_enable=c.cutv_enable,
        is_archived=c.is_archived,
        archive_content_id=archive_content_id,
        archive_content_type=archive_content_type,
        archive_published=archive_published,
        archive_scheduled_time=c.archive_scheduled_time,
        is_published=is_published,
        created_at=c.created_at,
        is_discarded=c.is_discarded,
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
    is_deleted: Optional[bool] = None,
    is_discarded: Optional[bool] = None,
    statuses: Optional[list[str]] = None,
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
        is_deleted      是否已删除（精确匹配，默认 False）
        statuses        Ingest 状态多选列表
        begin_from      开始时间下限（YYYY-MM-DD HH:MM）
        begin_to        开始时间上限
        end_from        结束时间下限
        end_to          结束时间上限

    输出：
        PaginatedResponse[ScheduleListItem]
    """
    logger.info(f"list_schedules 入参: page={page}, page_size={page_size}, title={title}, channel_id={channel_id}, channel_name={channel_name}, cutv_enable={cutv_enable}, cutv_enables={cutv_enables}, is_archived={is_archived}, is_deleted={is_deleted}, is_discarded={is_discarded}, statuses={statuses}, begin_from={begin_from}, begin_to={begin_to}, end_from={end_from}, end_to={end_to}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Content).where(
        Content.content_type == ContentType.SCHEDULE.value,
        Content.is_deleted.is_(is_deleted if is_deleted is not None else False),
    )
    # is_discarded 过滤：默认只显示未删除的，但允许前端传入参数查询已删除的
    if is_discarded is not None:
        query = query.where(Content.is_discarded.is_(is_discarded))
    else:
        query = query.where(Content.is_discarded.is_(False))
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
    if statuses:
        query = query.where(Content.status.in_(statuses))
    if channel_name:
        # 先查满足频道名称的 channel content_id，再过滤 parent_id
        channel_ids = (
            await db.execute(
                select(ChannelMetadata.content_id).where(
                    ChannelMetadata.name.ilike(f"%{channel_name}%"),
                    ChannelMetadata.is_deleted.is_(False),
                    ChannelMetadata.is_discarded.is_(False),
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
            query = query.order_by(Content.begin_time.desc(), Content.id.desc())
    else:
        query = query.order_by(Content.begin_time.desc(), Content.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    # 批量查询元数据名称
    content_ids = [c.id for c in rows]
    meta_rows = (
        await db.execute(
            select(ScheduleMetadata.content_id, ScheduleMetadata.name)
            .where(
                ScheduleMetadata.content_id.in_(content_ids),
                ScheduleMetadata.is_deleted.is_(False),
                ScheduleMetadata.is_discarded.is_(False),
            )
        )
    ).all()
    meta_map = {row.content_id: row.name for row in meta_rows}

    items = [await build_schedule_item(db, c, meta_map.get(c.id)) for c in rows]
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

    overlap = (
        await db.execute(
            select(Content.id).where(
                Content.content_type == ContentType.SCHEDULE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.parent_id == data.parent_id,
                Content.begin_time < data.end_time,
                Content.end_time > data.begin_time,
            ).limit(1)
        )
    ).scalar_one_or_none()
    if overlap:
        raise BusinessException(ErrorCode.SCHEDULE_TIME_CONFLICT, get_msg("SCHEDULE_TIME_CONFLICT"))

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

    return await build_schedule_item(db, schedule)


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
    return await build_schedule_item(db, schedule)


async def delete_schedule(db: AsyncSession, schedule_id: int) -> None:
    """
    软删除节目单。

    输入：schedule_id
    业务规则：复用内容删除逻辑，自动处理许可证/服务包/栏目的解绑
    """
    logger.info(f"delete_schedule 入参: schedule_id={schedule_id}")
    from app.internal.cms_biz_orchestration.services.content_service import delete_content
    await delete_content(db, schedule_id)


# ─── 节目单 Excel 导出/导入 ─────────────────────────────────────────────

async def export_schedules_excel(db: AsyncSession, ids: list[int]) -> bytes:
    """导出节目单为 Excel 文件（28 列，与导入模板一致，可直接用于导入）。"""
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

    # 批量获取 ScheduleMetadata
    content_ids = [r.id for r in rows]
    meta_map: dict[int, ScheduleMetadata] = {}
    if content_ids:
        metas = (
            await db.execute(
                select(ScheduleMetadata).where(ScheduleMetadata.content_id.in_(content_ids))
            )
        ).scalars().all()
        meta_map = {m.content_id: m for m in metas}

    # 批量获取 Genre（从 content_genre 中间表）
    content_genre_rows = (
        await db.execute(
            select(ContentGenre.content_id, ContentGenre.genre_id)
            .where(ContentGenre.content_id.in_(content_ids), ContentGenre.is_deleted.is_(False))
        )
    ).all()
    
    # 构建 content_id -> [genre_id] 的映射（一个内容可能有多个 genre）
    content_genre_map: dict[int, list[int]] = {}
    for cg_row in content_genre_rows:
        content_genre_map.setdefault(cg_row.content_id, []).append(cg_row.genre_id)
    
    # 收集所有 genre_id 和 type_id
    all_genre_ids: set[int] = set()
    for gids in content_genre_map.values():
        all_genre_ids.update(gids)
    type_ids_from_meta = [meta.type_id for meta in meta_map.values() if meta.type_id]
    genre_ids = list(all_genre_ids | set(type_ids_from_meta))
    genre_map: dict[int, str] = {}
    if genre_ids:
        g_rows = (await db.execute(select(Genre).where(Genre.id.in_(genre_ids)))).scalars().all()
        genre_map = {g.id: g.name for g in g_rows}

    # 批量获取 ContentType 名称（用于 type_id 匹配）
    type_ids = list({m.type_id for m in meta_map.values() if m.type_id})
    type_map: dict[int, str] = {}
    if type_ids:
        t_rows = (await db.execute(select(ContentTypeModel).where(ContentTypeModel.id.in_(type_ids)))).scalars().all()
        type_map = {t.id: t.name for t in t_rows}

    # 批量获取 Custom Tags（从 content_custom_tag 中间表）
    content_tag_rows = (
        await db.execute(
            select(ContentCustomTag.content_id, ContentCustomTag.custom_tag_id)
            .where(ContentCustomTag.content_id.in_(content_ids), ContentCustomTag.is_deleted.is_(False))
        )
    ).all()
    content_tag_map: dict[int, list[int]] = {}
    all_tag_ids: set[int] = set()
    for ct_row in content_tag_rows:
        content_tag_map.setdefault(ct_row.content_id, []).append(ct_row.custom_tag_id)
        all_tag_ids.add(ct_row.custom_tag_id)
    tag_name_map: dict[int, str] = {}
    if all_tag_ids:
        tag_rows = (await db.execute(select(CustomTag).where(CustomTag.id.in_(list(all_tag_ids))))).scalars().all()
        tag_name_map = {t.id: t.name for t in tag_rows}

    # 批量获取 Package（从 content_package 中间表）
    content_pkg_rows = (
        await db.execute(
            select(ContentPackage.content_id, ContentPackage.package_id)
            .where(ContentPackage.content_id.in_(content_ids), ContentPackage.is_deleted.is_(False))
        )
    ).all()
    content_pkg_map: dict[int, list[int]] = {}
    all_pkg_ids: set[int] = set()
    for cp_row in content_pkg_rows:
        content_pkg_map.setdefault(cp_row.content_id, []).append(cp_row.package_id)
        all_pkg_ids.add(cp_row.package_id)
    # 节目单的 Package ID 存储在 ScheduleMetadata.package_ids（保存路径不写中间表），需一并解析名称
    for meta in meta_map.values():
        if meta.package_ids:
            all_pkg_ids.update(meta.package_ids)
    pkg_name_map: dict[int, str] = {}
    if all_pkg_ids:
        pkg_rows = (await db.execute(select(Package).where(Package.id.in_(list(all_pkg_ids))))).scalars().all()
        pkg_name_map = {p.id: p.name for p in pkg_rows}

    # 字典字段导出 code→name（与导入端按名称匹配保持一致，避免导出编码）
    broadcast_type_map: dict[str, str] = {i.code: i.name for i in await get_dict_children_by_code(db, "BroadcastType")}
    rating_level_map: dict[str, str] = {i.code: i.name for i in await get_dict_children_by_code(db, "RatingLevel")}
    advice_map: dict[str, str] = {i.code: i.name for i in await get_dict_children_by_code(db, "Advice")}
    language_map: dict[str, str] = {i.code: i.name for i in await get_dict_children_by_code(db, "Language")}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Schedules"

    # 26 列：比导入模板（24 列）多 Status / CUTV Enable 两列只读信息，仅供查看/对账，
    # 导出文件不可直接再导入；Program ID/Series/Show 相关 8 列及 CDR ID、Tags、Studio 已移除
    headers = [
        "Content ID", "Program Name(*)", "Channel Name(*)", "Begin Time(*)", "End Time(*)",
        "BroadcastType", "RatingLevel(*)", "Advice",
        "Status", "SectionsInfo", "Description", "Audio Lang", "Subtitle Lang",
        "CUTV Enable", "TSTV Enable", "TSTV Mode",
        "NPVR Enable", "PPV Enable", "Pre Buffer", "Post Buffer",
        "Purchase Begin Time", "Purchase End Time",
        "Genre", "Package", "Custom Tags", "StatusFlag",
    ]
    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    # 富文本字体：字段名白色 + (*) 红色（写在同一单元格）
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    base_inline = InlineFont(rFont="Calibri", b=True, color="FFFFFF")
    mark_inline = InlineFont(rFont="Calibri", b=True, color="FF0000")

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col)
        if header.endswith("(*)"):
            cell.value = CellRichText(
                TextBlock(base_inline, header[:-3]),
                TextBlock(mark_inline, "(*)"),
            )
        else:
            cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    col_widths = [12, 30, 24, 22, 22,
                  16, 16, 16, 12,
                  30, 30, 16, 16,
                  14, 14, 14,
                  14, 14, 12, 12, 20, 20,
                  20, 24, 20, 14]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    def _arr_to_str(val: list[str] | None) -> str:
        """数组转逗号分隔字符串"""
        if not val:
            return ""
        return ",".join(str(v) for v in val)

    for row_idx, r in enumerate(rows, 2):
        m = meta_map.get(r.id)
        ws.cell(row=row_idx, column=1, value=r.id)
        ws.cell(row=row_idx, column=2, value=r.title)
        ws.cell(row=row_idx, column=3, value=channels.get(r.parent_id, ""))
        ws.cell(row=row_idx, column=4, value=r.begin_time.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if r.begin_time else "")
        ws.cell(row=row_idx, column=5, value=r.end_time.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if r.end_time else "")
        ws.cell(row=row_idx, column=6, value=broadcast_type_map.get(m.broadcast_type, m.broadcast_type) if m and m.broadcast_type else "")
        # 列 7=RatingLevel（注意：原代码在列 7 多写了一次 Genre，导致后续所有列错位一列，已修正）
        ws.cell(row=row_idx, column=7, value=rating_level_map.get(m.rating_level, m.rating_level) if m and m.rating_level else "")
        ws.cell(row=row_idx, column=8, value=_arr_to_str([advice_map.get(c, c) for c in m.advice]) if m and m.advice else "")
        ws.cell(row=row_idx, column=9, value=r.status or "")
        ws.cell(row=row_idx, column=10, value=json.dumps([{"type": s.get("type"), "action": s.get("action"), "tag": s.get("tag"), "start": s.get("start"), "end": s.get("end")} for s in m.sections_info], ensure_ascii=False) if m and m.sections_info else "")
        ws.cell(row=row_idx, column=11, value=m.description if m and m.description else "")
        ws.cell(row=row_idx, column=12, value=_arr_to_str([language_map.get(c, c) for c in m.audio_lang]) if m and m.audio_lang else "")
        ws.cell(row=row_idx, column=13, value=_arr_to_str([language_map.get(c, c) for c in m.subtitle_lang]) if m and m.subtitle_lang else "")
        ws.cell(row=row_idx, column=14, value="YES" if r.cutv_enable else "NO")
        ws.cell(row=row_idx, column=15, value="YES" if m and m.tstv_enable else "NO")
        ws.cell(row=row_idx, column=16, value="YES" if m and m.tstv_mode else "NO")
        ws.cell(row=row_idx, column=17, value="YES" if m and m.npvr_enable else "NO")
        ws.cell(row=row_idx, column=18, value="YES" if m and m.ppv_enable else "NO")
        # PPV Enable 关闭时，Pre Buffer/Post Buffer/Purchase Begin Time/Purchase End Time 不导出（避免显示默认值）
        if m and m.ppv_enable:
            ws.cell(row=row_idx, column=19, value=m.pre_buffer if m.pre_buffer is not None else "")
            ws.cell(row=row_idx, column=20, value=m.post_buffer if m.post_buffer is not None else "")
            ws.cell(row=row_idx, column=21, value=m.purchase_begin_time if m.purchase_begin_time is not None else "")
            ws.cell(row=row_idx, column=22, value=m.purchase_end_time if m.purchase_end_time is not None else "")
        else:
            ws.cell(row=row_idx, column=19, value="")
            ws.cell(row=row_idx, column=20, value="")
            ws.cell(row=row_idx, column=21, value="")
            ws.cell(row=row_idx, column=22, value="")
        # Genre（列 23）：多个 genre 用逗号拼接
        genre_ids_for_content = content_genre_map.get(r.id, [])
        genre_names_for_content = [genre_map.get(gid, "") for gid in genre_ids_for_content if genre_map.get(gid)]
        ws.cell(row=row_idx, column=23, value=",".join(genre_names_for_content) if genre_names_for_content else "")
        # Package（列 24）：合并 ScheduleMetadata.package_ids 与 content_package 中间表，去重保序
        pkg_ids: list[int] = list(m.package_ids) if m and m.package_ids else []
        for pid in content_pkg_map.get(r.id, []):
            if pid not in pkg_ids:
                pkg_ids.append(pid)
        pkg_names = [pkg_name_map.get(pid, "") for pid in pkg_ids if pkg_name_map.get(pid)]
        ws.cell(row=row_idx, column=24, value=",".join(pkg_names) if pkg_names else "")
        # Custom Tags（列 25）：从中间表批量查询，按 name 逗号拼接
        tag_ids = content_tag_map.get(r.id, [])
        tag_names = [tag_name_map.get(tid, "") for tid in tag_ids if tag_name_map.get(tid)]
        ws.cell(row=row_idx, column=25, value=",".join(tag_names) if tag_names else "")
        # StatusFlag（列 26）：meta 不存在时留空（None），存在时 YES/NO
        if m is not None:
            ws.cell(row=row_idx, column=26, value="YES" if m.status_flag else "NO")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


async def import_schedules_excel(
    db: AsyncSession,
    file: UploadFile,
    force: bool = False,
    processed_by: str | None = None,
) -> ScheduleImportResult:
    """从 Excel 导入节目单（含 ScheduleMetadata 全字段）。

    流程（需求 3.5.2.2 (7)）：
    - 以"内容ID"（Content ID）为唯一标识：
      - 指定了 Content ID 且存在 → 更新模式，以最新数据覆盖
      - 指定了 Content ID 但不存在 → 报错跳过，不自动创建
      - 未指定 Content ID → 新增模式，系统自动创建 Content（SCHEDULE 类型）
    - 对全部有效行（含更新行）根据 **频道 + 时间段** 检查冲突，冲突来源包括：
      ① 数据库已有节目（更新行排除自身）② 本次文件内其他行；
    - force=False 且发现冲突：不提交任何变更，返回 conflicts 列表，由前端提示用户是否覆盖；
    - force=True：
      - 数据库冲突 → 先软删除冲突的 SCHEDULE 记录，再插入新数据；
      - 文件内冲突 → 按"靠后行为准"：靠前行标记 dropped 跳过（计入 skipped），
        靠后行正常导入。
    - BroadcastType/Genre/RatingLevel/Advice/Language 等字段以文字匹配数据字典/基础数据，
      匹配不上时自动创建新子项/题材；自动创建失败（根节点不存在）则记录校验错误，该行跳过。
    - 严格按新模板（24 列）校验表头：缺失或多余列均直接拒绝导入，不兼容旧版文件；
      Program ID/Series/Show 相关 8 列及 CDR ID、Tags、Studio 已从模板移除；
      Status 与 CUTV Enable 亦不可导入：CUTV Enable 新建时固定为 False，
      Status 由业务流程（审核/发布等）变更，导入不修改这两个字段。
    - 必填字段前置校验（严格模式）：Program Name/Channel Name/Begin Time/End Time，
      以及 PPV Enable 为 YES 时的 Pre Buffer/Post Buffer/Package；
      任一行缺失必填字段 → 整个文件拒绝导入，不写入任何数据。
    - PPV Enable 为 YES 时，Pre Buffer / Post Buffer / Package 必填；为 NO 时可留空。
    - 逐行独立事务：某行失败仅回滚该行，不影响其他行。
    """
    from app.internal.cms_biz_system.models.dict import DictNode

    content = await file.read()
    wb = load_workbook(io.BytesIO(content), read_only=True)
    ws = wb.active

    # ── 表头校验（严格模式）───────────────────────────────
    # 导入模板表头（24 列）。模板中必填字段表头带红色 (*) 标记，匹配时忽略；
    # 严格按新模板执行：缺失或多余列均直接拒绝（含旧版的 Status/CUTV Enable 列）。
    expected_headers = [
        "Content ID", "Program Name", "Channel Name", "Begin Time", "End Time",
        "BroadcastType", "RatingLevel", "Advice",
        "SectionsInfo", "Description", "Audio Lang", "Subtitle Lang",
        "TSTV Enable", "TSTV Mode",
        "NPVR Enable", "PPV Enable", "Pre Buffer", "Post Buffer",
        "Purchase Begin Time", "Purchase End Time",
        "Genre", "Package", "Custom Tags", "StatusFlag",
    ]
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if not header_row:
        raise BusinessException(ErrorCode.INVALID_FILE_FORMAT, get_msg("INVALID_FILE_FORMAT"))
    actual_headers = [str(h).strip() if h is not None else "" for h in header_row]

    # 去除必填标记（(*) 或 *）与首尾空白后按表头名称匹配
    def _normalize_header(h: str) -> str:
        h = h.strip()
        if h.endswith("(*)"):
            return h[:-3].strip()
        return h.rstrip("*").strip()

    normalized_headers = [_normalize_header(h) for h in actual_headers]
    missing = [h for h in expected_headers if h not in normalized_headers]
    if missing:
        raise BusinessException(
            ErrorCode.INVALID_FILE_FORMAT,
            get_msg("INVALID_FILE_FORMAT") + f" (missing: {', '.join(missing)})",
        )
    # 严格模式：多余列同样拒绝（如旧版文件的 Studio/CDR ID/Tags/Program ID 等列）
    extra = [h for h in normalized_headers if h and h not in expected_headers]
    if extra:
        raise BusinessException(
            ErrorCode.INVALID_FILE_FORMAT,
            get_msg("INVALID_FILE_FORMAT") + f" (unexpected columns: {', '.join(extra)})",
        )

    # 表头名称 → 列索引映射：按名称取值（模板必填字段表头带 (*)，已归一化去除）
    header_map: dict[str, int] = {}
    for i, h in enumerate(normalized_headers):
        if h and h not in header_map:
            header_map[h] = i

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    result = ScheduleImportResult()
    # 预解析：收集有效行 + 冲突检测
    parsed: list[dict] = []
    conflicts: list = []
    # 文件内冲突检测用：未被靠后行覆盖（dropped=False）的已解析行区间
    kept_parsed: list[dict] = []
    from app.internal.cms_biz_orchestration.schemas.live import ScheduleImportConflict, ScheduleImportError

    # ── 预加载字典数据 ──────────────────────────────────────
    # 初始化字典缓存（与VOD导入一致，使用 {name: code} 映射）
    dict_caches: dict[str, dict[str, str]] = {}

    # Genre 匹配：{name_lower: Genre}（大小写不敏感）
    all_genres = (await db.execute(select(Genre).where(Genre.is_deleted.is_(False)))).scalars().all()
    genre_by_name: dict[str, Genre] = {g.name.lower(): g for g in all_genres}

    # 预加载所有频道（CHANNEL 类型 Content），用于按名称查找
    all_channels = (
        await db.execute(
            select(Content).where(
                Content.content_type == ContentType.CHANNEL.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().all()
    channel_by_name: dict[str, Content] = {c.title: c for c in all_channels}

    # 预加载所有 CustomTag，用于按名称严格匹配（不创建）
    all_tags = (await db.execute(select(CustomTag).where(CustomTag.is_deleted.is_(False)))).scalars().all()
    tag_by_name: dict[str, CustomTag] = {t.name: t for t in all_tags}

    # 预加载所有 Package，用于按名称严格匹配（不创建）
    all_packages = (await db.execute(select(Package).where(Package.is_deleted.is_(False)))).scalars().all()
    pkg_by_name: dict[str, Package] = {p.name: p for p in all_packages}

    # 辅助：按表头名称取列索引（列不存在时返回 None，读取值为空）
    def _col(name: str) -> int | None:
        return header_map.get(name)

    # 辅助：解析单元格
    def _cell(row: tuple, idx: int | None, default: str = "") -> str:
        if idx is not None and idx < len(row) and row[idx] is not None:
            return str(row[idx]).strip()
        return default

    def _bool_cell(row: tuple, idx: int) -> bool | None:
        val = _cell(row, idx).upper()
        if val in ("YES", "TRUE", "1"):
            return True
        if val in ("NO", "FALSE", "0"):
            return False
        return None

    def _int_cell(row: tuple, idx: int) -> int | None:
        val = _cell(row, idx)
        if not val:
            return None
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    # ── 必填字段前置校验（严格模式）───────────────────────
    # 任一行必填字段缺失（含 PPV Enable=YES 时的 Pre Buffer/Post Buffer/Package）
    # → 整个文件拒绝导入，直接抛错返回，不写入任何数据
    def _is_blank_row(row: tuple) -> bool:
        return not row or all(v is None or str(v).strip() == "" for v in row)

    required_errors: list[str] = []
    for idx, row in enumerate(rows, start=2):
        if _is_blank_row(row):
            continue
        missing_fields: list[str] = []
        if not _cell(row, _col("Program Name")):
            missing_fields.append("Program Name")
        if not _cell(row, _col("Channel Name")):
            missing_fields.append("Channel Name")
        if not _cell(row, _col("Begin Time")):
            missing_fields.append("Begin Time")
        if not _cell(row, _col("End Time")):
            missing_fields.append("End Time")
        if not _cell(row, _col("RatingLevel")):
            missing_fields.append("RatingLevel")
        if _bool_cell(row, _col("PPV Enable")) is True:
            if _int_cell(row, _col("Pre Buffer")) is None:
                missing_fields.append("Pre Buffer")
            if _int_cell(row, _col("Post Buffer")) is None:
                missing_fields.append("Post Buffer")
            if not _cell(row, _col("Package")):
                missing_fields.append("Package")
        if missing_fields:
            required_errors.append(f"Row {idx}: {', '.join(missing_fields)}")

    if required_errors:
        # 错误过多时最多展示 20 行，避免提示过长
        shown = required_errors[:20]
        suffix = f" ...（共 {len(required_errors)} 行缺失必填字段）" if len(required_errors) > 20 else ""
        raise BusinessException(
            ErrorCode.EXCEL_IMPORT_FAILED,
            get_msg("EXCEL_IMPORT_FAILED") + ": " + "; ".join(shown) + suffix,
        )

    for idx, row in enumerate(rows, start=2):
        if _is_blank_row(row):  # 完全空行：静默跳过
            continue
        title = _cell(row, _col("Program Name"))

        id_raw = _cell(row, _col("Content ID"))
        id_val = int(id_raw) if id_raw.isdigit() else None
        channel_name = _cell(row, _col("Channel Name"))
        begin_str = _cell(row, _col("Begin Time"))
        end_str = _cell(row, _col("End Time"))
        rating_level_raw = _cell(row, _col("RatingLevel"))

        # ── 行级基础校验 ──────────────────────────────────
        row_errors: list[str] = []

        if not title:
            row_errors.append("Program Name 不能为空")
        if not begin_str:
            row_errors.append("Begin Time 不能为空")
        if not end_str:
            row_errors.append("End Time 不能为空")
        if not rating_level_raw:
            row_errors.append("RatingLevel 不能为空")

        begin_time = None
        end_time = None
        if begin_str and end_str:
            try:
                begin_time = datetime.strptime(begin_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=app_tz)
            except ValueError:
                row_errors.append(f"Begin Time 格式错误: '{begin_str}'，应为 YYYY-MM-DD HH:MM:SS")
            try:
                end_time = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=app_tz)
            except ValueError:
                row_errors.append(f"End Time 格式错误: '{end_str}'，应为 YYYY-MM-DD HH:MM:SS")

        if begin_time and end_time and begin_time >= end_time:
            row_errors.append("Begin Time 必须早于 End Time")

        # 频道校验
        channel = None
        if channel_name:
            channel = channel_by_name.get(channel_name)
            if not channel:
                row_errors.append(f"频道 '{channel_name}' 不存在")
        else:
            row_errors.append("Channel Name 不能为空")

        # ── 字典值严格匹配（不自动创建）──────────────────────
        # 按表头名称取列（模板必填字段表头带 *，header_map 中已去除）
        broadcast_type_raw = _cell(row, _col("BroadcastType"))
        advice_raw = _cell(row, _col("Advice"))
        sections_info_raw = _cell(row, _col("SectionsInfo"))
        audio_lang_raw = _cell(row, _col("Audio Lang"))
        subtitle_lang_raw = _cell(row, _col("Subtitle Lang"))
        genre_name = _cell(row, _col("Genre"))
        package_name = _cell(row, _col("Package"))
        custom_tags_raw = _cell(row, _col("Custom Tags"))
        # PPV 相关字段（PPV Enable 为 YES 时 Pre Buffer/Post Buffer/Package 必填）
        ppv_enable_val = _bool_cell(row, _col("PPV Enable"))
        pre_buffer_val = _int_cell(row, _col("Pre Buffer"))
        post_buffer_val = _int_cell(row, _col("Post Buffer"))
        purchase_begin_val = _int_cell(row, _col("Purchase Begin Time"))
        purchase_end_val = _int_cell(row, _col("Purchase End Time"))

        # 使用公共函数进行严格匹配
        broadcast_type_code = await resolve_single_dict_code_strict(db, "BroadcastType", broadcast_type_raw, dict_caches, row_errors, "BroadcastType")
        rating_level_code = await resolve_single_dict_code_strict(db, "RatingLevel", rating_level_raw, dict_caches, row_errors, "RatingLevel")
        advice_codes = await resolve_dict_codes_strict(db, "Advice", advice_raw, dict_caches, row_errors, "Advice")
        audio_lang_codes = await resolve_dict_codes_strict(db, "Language", audio_lang_raw, dict_caches, row_errors, "AudioLang")
        subtitle_lang_codes = await resolve_dict_codes_strict(db, "Language", subtitle_lang_raw, dict_caches, row_errors, "SubtitleLang")

        sections_info = None
        if sections_info_raw:
            try:
                sections_info = json.loads(sections_info_raw)
            except (json.JSONDecodeError, TypeError):
                row_errors.append(f"SectionsInfo 格式错误，应为 JSON 数组")
            if sections_info is not None:
                # 按 C2 规范校验并归一化，阻止字符串格式脏数据入库（会导致详情接口 500）
                sections_info, section_errors = normalize_sections_info(sections_info)
                row_errors.extend(section_errors)

        # Genre 严格匹配（大小写不敏感）：匹配不上不创建，记录错误
        genre_id: int | None = None
        if genre_name:
            genre_obj = genre_by_name.get(genre_name.lower())
            if genre_obj:
                genre_id = genre_obj.id
            else:
                row_errors.append(f"Genre not found: {genre_name}")

        # Custom Tags 严格匹配（列 35，逗号分隔）：匹配不上不创建，记录错误
        custom_tag_ids: list[int] | None = None
        if custom_tags_raw:
            tag_items = [t.strip() for t in str(custom_tags_raw).split(",") if t.strip()]
            if tag_items:
                matched_ids: list[int] = []
                all_matched = True
                for item in tag_items:
                    tag_obj = tag_by_name.get(item)
                    if tag_obj:
                        matched_ids.append(tag_obj.id)
                    else:
                        row_errors.append(f"Custom Tags not found: {item}")
                        all_matched = False
                custom_tag_ids = matched_ids if all_matched else None

        # StatusFlag 解析：YES/NO → bool，空值不覆盖
        status_flag_val = _bool_cell(row, _col("StatusFlag"))

        # Package 解析（逗号分隔，按名称严格匹配，不创建）→ 更新 ScheduleMetadata.package_ids
        package_ids_resolved: list[int] = []
        if package_name:
            for pn in [p.strip() for p in package_name.split(",") if p.strip()]:
                pkg_obj = pkg_by_name.get(pn)
                if pkg_obj:
                    package_ids_resolved.append(pkg_obj.id)
                else:
                    row_errors.append(f"Package not found: {pn}")

        # PPV Enable 为 YES 时，Pre Buffer / Post Buffer / Package 必填
        if ppv_enable_val is True:
            if pre_buffer_val is None:
                row_errors.append("PPV Enable 为 YES 时，Pre Buffer 不能为空")
            if post_buffer_val is None:
                row_errors.append("PPV Enable 为 YES 时，Post Buffer 不能为空")
            if not package_ids_resolved and not package_name:
                row_errors.append("PPV Enable 为 YES 时，Package 不能为空")

        # 如果有校验错误，跳过该行并记录
        if row_errors:
            result.errors.append(ScheduleImportError(row=idx, errors=row_errors))
            result.skipped += 1
            continue

        existing = None
        existing_meta = None
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
            if not existing:
                # 指定了 Content ID 但不存在 → 报错，不自动创建
                result.errors.append(ScheduleImportError(
                    row=idx,
                    errors=[f"内容ID {id_val} 不存在，无法更新"],
                ))
                result.skipped += 1
                continue
            existing_meta = (
                await db.execute(
                    select(ScheduleMetadata).where(ScheduleMetadata.content_id == existing.id)
                )
            ).scalar_one_or_none()

        # 行校验全部通过，计入有效总数
        result.total += 1

        # 收集元数据字段
        # 注：Status 与 CUTV Enable 不在导入模板中 ——
        # Status 由业务流程变更（新建时默认 None，更新时保持原值），
        # CUTV Enable 新建固定 False，更新时保持原值。
        meta_fields = {
            "broadcast_type": broadcast_type_code,
            "genre_id": genre_id,
            "rating_level": rating_level_code,
            "advice": advice_codes,
            "sections_info": sections_info,
            "description": _cell(row, _col("Description")),
            "audio_lang": audio_lang_codes,
            "subtitle_lang": subtitle_lang_codes,
            "tstv_enable": _bool_cell(row, _col("TSTV Enable")),
            "tstv_mode": _bool_cell(row, _col("TSTV Mode")),
            "npvr_enable": _bool_cell(row, _col("NPVR Enable")),
            "ppv_enable": ppv_enable_val,
            "pre_buffer": pre_buffer_val,
            "post_buffer": post_buffer_val,
            "purchase_begin_time": purchase_begin_val,
            "purchase_end_time": purchase_end_val,
            "custom_tag_ids": custom_tag_ids,
            "package_ids": package_ids_resolved if package_ids_resolved else None,  # Package
            "status_flag": status_flag_val,
        }

        parsed.append({
            "row": idx,
            "id": id_val,
            "title": title,
            "channel": channel,
            "begin": begin_time,
            "end": end_time,
            "existing": existing,
            "existing_meta": existing_meta,
            "meta_fields": meta_fields,
            "dropped": False,  # 被文件内靠后行覆盖时置 True，导入时跳过
        })

        # ── 冲突检测（所有有效行，含更新行）：数据库已有 + 文件内已解析行 ──
        # ① 数据库已有节目：新增行直接比对；更新行排除自身（Content.id != id_val）
        overlap_query = (
            select(Content.id, Content.title)
            .where(
                Content.content_type == ContentType.SCHEDULE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.parent_id == channel.id,
                Content.begin_time < end_time,
                Content.end_time > begin_time,
            )
        )
        if id_val is not None:
            overlap_query = overlap_query.where(Content.id != id_val)
        overlap_rows = (await db.execute(overlap_query)).all()
        if overlap_rows:
            conflicts.append(ScheduleImportConflict(
                row=idx,
                channel_name=channel_name,
                title=title,
                begin_time=begin_str,
                end_time=end_str,
                conflict_ids=[r[0] for r in overlap_rows],
                conflict_source="existing",
            ))

        # ② 文件内已解析且未被覆盖跳过的行：同频道且时间段重叠 → 按"靠后行为准"策略，
        #    靠前行被靠后行覆盖：标记 dropped，不参与导入，计入 skipped
        still_kept: list[dict] = []
        for prev in kept_parsed:
            if prev["channel"].id == channel.id and prev["begin"] < end_time and prev["end"] > begin_time:
                prev["dropped"] = True
                result.total -= 1
                result.skipped += 1
                result.errors.append(ScheduleImportError(
                    row=prev["row"],
                    errors=[f"频道+时间段与第 {idx} 行冲突，按靠后行覆盖策略该行未导入"],
                ))
                conflicts.append(ScheduleImportConflict(
                    row=idx,
                    channel_name=channel_name,
                    title=title,
                    begin_time=begin_str,
                    end_time=end_str,
                    conflict_ids=[],
                    conflict_source="in_file",
                    conflict_row=prev["row"],
                ))
            else:
                still_kept.append(prev)
        kept_parsed[:] = still_kept
        kept_parsed.append({
            "row": idx,
            "channel": channel,
            "begin": begin_time,
            "end": end_time,
        })

    # 文件内冲突已被"靠后行为准"解决的行（dropped=True）不再导入，
    # 其与其他数据的冲突也无需用户决策，从冲突列表中剔除
    dropped_rows = {item["row"] for item in parsed if item["dropped"]}
    conflicts = [c for c in conflicts if c.row not in dropped_rows]

    # 有冲突且非强制覆盖→ 不提交，仅返回 conflicts
    if conflicts and not force:
        result.conflicts = conflicts
        result.created = 0
        result.updated = 0
        return result

    # 已回退过的父级频道集合：同一频道在一次导入中仅回退一次
    # （回退后频道为 InProgress，重复调用无实际效果，去重仅为减少查询）
    rolled_back_parents: set[int] = set()

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
                # 强制覆盖删除冲突节目单属于频道节点数据变更 → 回退祖先状态
                if v.parent_id and v.parent_id not in rolled_back_parents:
                    rolled_back_parents.add(v.parent_id)
                    logger.info(
                        f"[节目单导入] 强制覆盖删除冲突节目单 #{v.id}，回退所属频道 #{v.parent_id} 祖先状态"
                    )
                    await rollback_ancestors_after_child_change(
                        db,
                        start_parent_id=v.parent_id,
                        edited_by="import",
                        edit_info=f"Excel导入强制覆盖删除节目单「{v.title}」",
                    )
            await db.flush()

        # 若更新行的目标记录本身在强制删除范围内（被其他行覆盖），该更新行跳过，
        # 避免把已被软删除的记录再更新出"已删除但仍被修改"的状态
        for item in parsed:
            if not item["dropped"] and item["existing"] is not None and item["existing"].id in conflict_id_set:
                item["dropped"] = True
                result.total -= 1
                result.skipped += 1
                result.errors.append(ScheduleImportError(
                    row=item["row"],
                    errors=[f"内容ID {item['existing'].id} 已被本次导入强制覆盖删除，该行未导入"],
                ))

    # 逐行独立事务提交数据
    for item in parsed:
        if item["dropped"]:
            continue  # 被文件内靠后行覆盖（或目标记录被覆盖删除），跳过导入
        channel = item["channel"]
        existing = item["existing"]
        existing_meta = item["existing_meta"]
        mf = item["meta_fields"]

        try:
            if existing is not None:
                # 更新 Content 主表（Status 与 CUTV Enable 不可导入：保持原值，由业务流程变更）
                existing.title = item["title"]
                existing.parent_id = channel.id
                existing.begin_time = item["begin"]
                existing.end_time = item["end"]
                if mf["genre_id"] is not None:
                    # 更新 ContentGenre 中间表（替换为新的题材）
                    await db.execute(
                        ContentGenre.__table__.delete().where(
                            ContentGenre.content_id == existing.id,
                        )
                    )
                    db.add(ContentGenre(
                        content_id=existing.id,
                        genre_id=mf["genre_id"],
                    ))

                # 更新 ContentCustomTag 中间表（替换为新的标签）
                if mf["custom_tag_ids"] is not None:
                    await db.execute(
                        ContentCustomTag.__table__.delete().where(
                            ContentCustomTag.content_id == existing.id,
                        )
                    )
                    for tid in mf["custom_tag_ids"]:
                        db.add(ContentCustomTag(
                            content_id=existing.id,
                            custom_tag_id=tid,
                        ))

                # 更新 ScheduleMetadata
                if existing_meta:
                    if mf["broadcast_type"] is not None:
                        existing_meta.broadcast_type = mf["broadcast_type"]
                    if mf["rating_level"] is not None:
                        existing_meta.rating_level = mf["rating_level"]
                    if mf["advice"] is not None:
                        existing_meta.advice = mf["advice"]
                    if mf["sections_info"] is not None:
                        existing_meta.sections_info = mf["sections_info"]
                    if mf["description"]:
                        existing_meta.description = mf["description"]
                    if mf["audio_lang"] is not None:
                        existing_meta.audio_lang = mf["audio_lang"]
                    if mf["subtitle_lang"] is not None:
                        existing_meta.subtitle_lang = mf["subtitle_lang"]
                    if mf["tstv_enable"] is not None:
                        existing_meta.tstv_enable = mf["tstv_enable"]
                    if mf["tstv_mode"] is not None:
                        existing_meta.tstv_mode = mf["tstv_mode"]
                    if mf["npvr_enable"] is not None:
                        existing_meta.npvr_enable = mf["npvr_enable"]
                    if mf["ppv_enable"] is not None:
                        existing_meta.ppv_enable = mf["ppv_enable"]
                    if mf["pre_buffer"] is not None:
                        existing_meta.pre_buffer = mf["pre_buffer"]
                    if mf["post_buffer"] is not None:
                        existing_meta.post_buffer = mf["post_buffer"]
                    if mf["purchase_begin_time"] is not None:
                        existing_meta.purchase_begin_time = mf["purchase_begin_time"]
                    if mf["purchase_end_time"] is not None:
                        existing_meta.purchase_end_time = mf["purchase_end_time"]
                    if mf["status_flag"] is not None:
                        existing_meta.status_flag = mf["status_flag"]
                    # Package 更新（ScheduleMetadata.package_ids）
                    if mf["package_ids"] is not None:
                        existing_meta.package_ids = mf["package_ids"]
                    existing_meta.name = item["title"]
                else:
                    # 不存在 meta 则创建（cdr_id 列 NOT NULL，自动生成规则与 create_schedule_metadata 一致）
                    db.add(ScheduleMetadata(
                        content_id=existing.id,
                        name=item["title"],
                        cdr_id=f"Schedule_{existing.id}",
                        broadcast_type=mf["broadcast_type"],
                        rating_level=mf["rating_level"],
                        advice=mf["advice"],
                        sections_info=mf["sections_info"],
                        description=mf["description"] or None,
                        audio_lang=mf["audio_lang"],
                        subtitle_lang=mf["subtitle_lang"],
                        tstv_enable=mf["tstv_enable"] if mf["tstv_enable"] is not None else True,
                        tstv_mode=mf["tstv_mode"] if mf["tstv_mode"] is not None else False,
                        npvr_enable=mf["npvr_enable"] if mf["npvr_enable"] is not None else True,
                        ppv_enable=mf["ppv_enable"] if mf["ppv_enable"] is not None else False,
                        pre_buffer=mf["pre_buffer"] if mf["pre_buffer"] is not None else 0,
                        post_buffer=mf["post_buffer"] if mf["post_buffer"] is not None else 0,
                        purchase_begin_time=mf["purchase_begin_time"] if mf["purchase_begin_time"] is not None else 180,
                        purchase_end_time=mf["purchase_end_time"] if mf["purchase_end_time"] is not None else -1,
                        cutv_enable=False,  # CUTV Enable 不可导入，固定默认 False
                        status_flag=mf["status_flag"] if mf["status_flag"] is not None else True,
                        package_ids=mf["package_ids"],  # Package
                    ))
                    await complete_process_and_update_status(
                        db, content_id=existing.id, content_type="SCHEDULE",
                        process_name="Metadata", processed_by=processed_by or "import",
                    )

                # Excel 导入覆盖节点数据 → 已发布/准备发布等内容回退状态重新走审核
                await rollback_after_published_edit(
                    db, existing.id, existing.content_type, processed_by or "import", "Excel导入更新节目单"
                )
                await write_log(
                    db,
                    user_id=None,
                    user_name=processed_by,
                    operation_type=OperationType.SCHEDULE_UPDATE,
                    operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": item["title"]},
                    operation_content_code="LOG_SCHEDULE_UPDATE", operation_content_params={"title": item["title"]},
                    content_id=existing.id,
                    entity_type="content",
                    entity_id=existing.id,
                    result="success",
                )
                result.updated += 1
            else:
                # 新增：创建 Content + ScheduleMetadata
                # Status 由业务流程管理（默认 None）；CUTV Enable 不可导入，固定 False
                new_content = Content(
                    content_type=ContentType.SCHEDULE.value,
                    title=item["title"],
                    status=ContentStatus.NONE.value,
                    parent_id=channel.id,
                    begin_time=item["begin"],
                    end_time=item["end"],
                    cutv_enable=False,
                )
                db.add(new_content)
                await db.flush()  # 获取 new_content.id

                if mf["genre_id"] is not None:
                    db.add(ContentGenre(
                        content_id=new_content.id,
                        genre_id=mf["genre_id"],
                    ))

                # 新增 ContentCustomTag 中间表
                if mf["custom_tag_ids"] is not None:
                    for tid in mf["custom_tag_ids"]:
                        db.add(ContentCustomTag(
                            content_id=new_content.id,
                            custom_tag_id=tid,
                        ))

                db.add(ScheduleMetadata(
                    content_id=new_content.id,
                    name=item["title"],
                    cdr_id=f"Schedule_{new_content.id}",
                    broadcast_type=mf["broadcast_type"],
                    rating_level=mf["rating_level"],
                    advice=mf["advice"],
                    sections_info=mf["sections_info"],
                    description=mf["description"] or None,
                    audio_lang=mf["audio_lang"],
                    subtitle_lang=mf["subtitle_lang"],
                    cutv_enable=False,  # CUTV Enable 不可导入，固定默认 False
                    tstv_enable=mf["tstv_enable"] if mf["tstv_enable"] is not None else True,
                    tstv_mode=mf["tstv_mode"] if mf["tstv_mode"] is not None else False,
                    npvr_enable=mf["npvr_enable"] if mf["npvr_enable"] is not None else True,
                    ppv_enable=mf["ppv_enable"] if mf["ppv_enable"] is not None else False,
                    pre_buffer=mf["pre_buffer"] if mf["pre_buffer"] is not None else 0,
                    post_buffer=mf["post_buffer"] if mf["post_buffer"] is not None else 0,
                    purchase_begin_time=mf["purchase_begin_time"] if mf["purchase_begin_time"] is not None else 180,
                    purchase_end_time=mf["purchase_end_time"] if mf["purchase_end_time"] is not None else -1,
                    status_flag=mf["status_flag"] if mf["status_flag"] is not None else True,
                    package_ids=mf["package_ids"],  # Package
                ))
                await complete_process_and_update_status(
                    db, content_id=new_content.id, content_type="SCHEDULE",
                    process_name="Metadata", processed_by=processed_by or "import",
                )
                # 新建节目单创建 arrangement 任务，与手动创建路径对齐
                await create_arrangement_task(db, new_content.id, processed_by=processed_by)
                # 新增节目单属于频道节点数据变更 → 回退祖先状态（同一频道仅回退一次）
                if channel.id not in rolled_back_parents:
                    rolled_back_parents.add(channel.id)
                    logger.info(
                        f"[节目单导入] 新增节目单 #{new_content.id}，回退所属频道 #{channel.id} 祖先状态"
                    )
                    await rollback_ancestors_after_child_change(
                        db,
                        start_parent_id=channel.id,
                        edited_by="import",
                        edit_info=f"Excel导入新增节目单「{item['title']}」",
                    )
                await write_log(
                    db,
                    user_id=None,
                    user_name=processed_by,
                    operation_type=OperationType.SCHEDULE_CREATE,
                    operation_object_code="OBJ_SCHEDULE", operation_object_params={"name": item["title"]},
                    operation_content_code="LOG_SCHEDULE_CREATE", operation_content_params={"title": item["title"]},
                    content_id=new_content.id,
                    entity_type="content",
                    entity_id=new_content.id,
                    result="success",
                )
                result.created += 1

            # 逐行提交事务
            await db.commit()

        except BusinessException as e:
            # 业务校验失败（如名称重复）→ 行级跳过并透传具体原因
            await db.rollback()
            result.errors.append(ScheduleImportError(
                row=item["row"],
                errors=[str(e)],
            ))
            result.skipped += 1
        except Exception:
            # 该行失败则回滚该行并记录错误
            await db.rollback()
            result.errors.append(ScheduleImportError(
                row=item["row"],
                errors=[f"写入数据库失败"],
            ))
            result.skipped += 1
            # 回滚后需要重新开始一个事务供后续行使用
            # （SQLAlchemy 在 rollback 后会自动重新开始事务）

    return result


# ─── 归档管理（已归档的 VOD 内容）───────────────────────────────────────

_ARCHIVE_TYPES = [ContentType.MOVIE.value, ContentType.EPISODE.value, ContentType.SEASON.value, ContentType.SERIES.value, ContentType.SEASON_SERIES.value]


async def archive_schedule(
    db: AsyncSession,
    data: ArchiveRequest,
    processed_by: str | None = None,
) -> ArchiveResponse:
    """
    归档节目单：根据 ScheduleMetadata.series_type 创建对应归档产物。

    归档逻辑（需求 3.5.2 节目归档分支）：
        SeriesType=0 → 创建 MOVIE（is_archived=true, source_schedule_id=schedule.id）
        SeriesType=1 → 查找/创建 SERIES（独立连续剧） → 在其下创建 EPISODE
        SeriesType=2 → 查找/创建 SEASON → 查找/创建 SEASON_SERIES（单季） → 在其下创建 EPISODE

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

    # 校验节目单是否已发布，未发布不能归档
    from app.internal.cms_biz_publish.repositories import publish_repository
    publish_status = await publish_repository.get_object_publish_status(
        db, "Content", data.schedule_id
    )
    if not publish_status or not publish_status.is_published:
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("SCHEDULE_NOT_PUBLISHED_CANNOT_ARCHIVE"),
            400
        )

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
        raise BusinessException(
            ErrorCode.SCHEDULE_ALREADY_ARCHIVED,
            get_msg("SCHEDULE_ALREADY_ARCHIVED"),
            400
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

    # 2.0 兼容历史脏数据：早期创建的 schedule_metadata 可能 cdr_id 为空
    # （该列在部分 schema 实际可空，与 ORM 模型 nullable=False 不一致），
    # 归档时若为空则按 Schedule_{id} 补齐；只填空、不覆盖已有值，向后兼容。
    if sched_meta is not None and not sched_meta.cdr_id:
        sched_meta.cdr_id = f"Schedule_{data.schedule_id}"
        await db.flush()
        logger.info(f"归档: schedule_metadata id={sched_meta.id} cdr_id 为空，补齐为 {sched_meta.cdr_id}")

    # 2.1 应用归档弹窗随传的元数据（仅内存赋值，随归档同事务提交；
    # 后续 Sequence 等校验失败抛异常时整体回滚，不会单独落库）
    meta_override = False
    if data.mode == "now":
        override_fields = {
            "series_type": data.series_type,
            "series_name": data.series_name,
            "series_id": data.series_id,
            "sequence": data.sequence,
            "series_ordinal": data.series_ordinal,
            "show_name": data.show_name,
            "show_id": data.show_id,
            "program_id": data.program_id,
            "cutv_enable": data.cutv_enable,
        }
        if sched_meta is None and any(v is not None for v in override_fields.values()):
            # cdr_id 列 NOT NULL，与 create_schedule_metadata / 导入路径统一按 Schedule_{id} 生成，
            # 否则会造出 cdr_id 为空的 schedule_metadata 脏数据
            sched_meta = ScheduleMetadata(
                content_id=data.schedule_id,
                name=schedule.title,
                cdr_id=f"Schedule_{data.schedule_id}",
            )
            db.add(sched_meta)
            await db.flush()
        if sched_meta is not None:
            for key, value in override_fields.items():
                if value is not None:
                    setattr(sched_meta, key, value)
                    meta_override = True

    series_type = sched_meta.series_type if sched_meta else 0

    # 从 content_genre 中间表获取归档节目的题材
    archive_genre_ids: list[int] = []
    genre_rows = (await db.execute(
        select(ContentGenre.genre_id).where(
            ContentGenre.content_id == data.schedule_id,
            ContentGenre.is_deleted.is_(False),
        )
    )).all()
    archive_genre_ids = [r[0] for r in genre_rows]

    def _add_genre_records(cid: int) -> None:
        """为归档产物创建 ContentGenre 记录"""
        for gid in archive_genre_ids:
            db.add(ContentGenre(
                content_id=cid,
                genre_id=gid,
                created_by=schedule.created_by,
            ))

    # 3. 根据 series_type 创建归档产物
    archive_content_id: Optional[int] = None
    archive_content_type: Optional[str] = None

    if series_type == 0:
        # SeriesType=0: 独立 program → 直接创建 MOVIE
        movie = Content(
            content_type=ContentType.MOVIE.value,
            title=schedule.title,
            status=ContentStatus.NONE.value,
            is_archived=True,
            source_schedule_id=data.schedule_id,
        )
        db.add(movie)
        await db.flush()
        _add_genre_records(movie.id)
        # 继承 SCHEDULE 的许可证
        await _inherit_licenses_from(db, schedule.id, movie.id)
        archive_content_id = movie.id
        archive_content_type = ContentType.MOVIE.value

        logger.info(f"归档: SeriesType=0, 创建 MOVIE id={movie.id}")

    elif series_type == 1:
        # SeriesType=1: 普通连续剧单集 → 查找/创建 SERIES → 创建 EPISODE
        series_content = await _find_or_create_series(
            db, sched_meta, schedule, data.schedule_id
        )
        # SERIES 继承 SCHEDULE 的许可证
        await _inherit_licenses_from(db, schedule.id, series_content.id)
        ep_sequence = sched_meta.sequence if sched_meta else None
        if ep_sequence is not None:
            await _check_episode_duplicate(db, series_content.id, ep_sequence)
        episode = Content(
            content_type=ContentType.EPISODE.value,
            title=_build_episode_title(sched_meta, schedule),
            status=ContentStatus.NONE.value,
            parent_id=series_content.id,
            sequence=ep_sequence,
            is_archived=True,
            source_schedule_id=data.schedule_id,
        )
        db.add(episode)
        await db.flush()
        _add_genre_records(episode.id)
        # EPISODE 继承 SERIES 的许可证
        await _inherit_licenses_from(db, series_content.id, episode.id)
        archive_content_id = episode.id
        archive_content_type = ContentType.EPISODE.value

        # 自动维护 VolumnCount：统计 SERIES 下 EPISODE 数
        await _update_volume_count(db, series_content.id)

        logger.info(f"归档: SeriesType=1, SERIES id={series_content.id}, EPISODE id={episode.id}")

    elif series_type == 2:
        # SeriesType=2: 分季连续剧单集 → 查找/创建 SEASON → 查找/创建 SERIES → 创建 EPISODE
        # 优先按单季名称查找已有单季并复用其所在总季：
        # 归档导入多行同剧时 series_id/show_id 均为空，若每行都新建 SEASON/SEASON_SERIES
        # 会产生重复树（同名单季各自挂在不同总季下）；名称匹配与 VOD 导入 Parent Name 口径一致
        _ss_name = sched_meta.series_name if sched_meta else None
        _existing_ss = await _find_series_by_name_under_season(db, _ss_name)
        if _existing_ss is not None:
            _season = await db.get(Content, _existing_ss.parent_id)
            if _season is None or _season.is_deleted or _season.is_discarded:
                _existing_ss = None  # 父总季已失效，走新建路径
        if _existing_ss is not None:
            season_content = await db.get(Content, _existing_ss.parent_id)
            series_content = _existing_ss
        else:
            season_content = await _find_or_create_season(
                db, sched_meta, schedule, data.schedule_id
            )
            series_content = await _find_or_create_series_under_season(
                db, sched_meta, season_content, schedule, data.schedule_id
            )
        # SEASON 继承 SCHEDULE 的许可证（幂等，复用时不会重复插入）
        await _inherit_licenses_from(db, schedule.id, season_content.id)
        # 单季继承总季的许可证
        await _inherit_licenses_from(db, season_content.id, series_content.id)
        ep_sequence = sched_meta.sequence if sched_meta else None
        if ep_sequence is not None:
            await _check_episode_duplicate(db, series_content.id, ep_sequence)
        episode = Content(
            content_type=ContentType.EPISODE.value,
            title=_build_episode_title(sched_meta, schedule),
            status=ContentStatus.NONE.value,
            parent_id=series_content.id,
            sequence=ep_sequence,
            is_archived=True,
            source_schedule_id=data.schedule_id,
        )
        db.add(episode)
        await db.flush()
        _add_genre_records(episode.id)
        # EPISODE 继承单季的许可证
        await _inherit_licenses_from(db, series_content.id, episode.id)
        archive_content_id = episode.id
        archive_content_type = ContentType.EPISODE.value

        # 自动维护 VolumnCount：统计 SERIES 下 EPISODE 数
        await _update_volume_count(db, series_content.id)

        logger.info(f"归档: SeriesType=2, SEASON id={season_content.id}, SERIES id={series_content.id}, EPISODE id={episode.id}")
    else:
        raise BusinessException(ErrorCode.INVALID_SERIES_TYPE, get_msg("INVALID_SERIES_TYPE", series_type=series_type))

    # 4. 标记 SCHEDULE 为已归档
    schedule.is_archived = True

    # 5. 从 ScheduleMetadata 复制元数据到 ProgramMetadata
    if sched_meta is not None and archive_content_id is not None:
        await _copy_schedule_metadata_to_program(db, sched_meta, archive_content_id, archive_content_type, series_type)

    # 6. 为归档产物创建 arrangement 任务
    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(db, archive_content_id)

    # 7. 归档成功后：无条件回写 program_id（归档产物指针，与是否覆盖字段无关）；
    #    cutv_enable 同步与"已发布编辑回滚"状态流转仅在传入覆盖字段时执行
    # （与归档同事务；归档失败时不会执行到此，状态保持不变）
    if sched_meta is not None and archive_content_id is not None:
        sched_meta.program_id = str(archive_content_id)
    if meta_override and sched_meta is not None:
        if sched_meta.cutv_enable is not None:
            schedule.cutv_enable = sched_meta.cutv_enable
        await rollback_after_published_edit(
            db,
            content_id=schedule.id,
            content_type=schedule.content_type,
            edited_by=processed_by or "system",
            edit_info="归档编辑元数据",
        )
        await complete_process_and_update_status(
            db,
            content_id=schedule.id,
            content_type=schedule.content_type,
            process_name="Metadata",
            processed_by=processed_by,
            record_status="Passed",
            info="归档更新元数据",
        )

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

    content_type 必须传入明确的单一类型（调用方按 series_type 语义锁定：
    普通连续剧传 SERIES，分季单季传 SEASON_SERIES），避免或查询匹配到错误层级内容。

    查找优先级：
    1. 若 id_str 可转为整数，优先按 Content.id 精确匹配
    2. 按 Content.external_id 精确匹配
    """
    conditions = [
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
        Content.content_type == content_type,
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


async def _copy_genre_from_schedule(
    db: AsyncSession,
    schedule_id: int,
    target_content_id: int,
) -> None:
    """从 schedule 复制题材到目标内容（通过 ContentGenre 中间表）。"""
    genre_rows = (await db.execute(
        select(ContentGenre.genre_id).where(
            ContentGenre.content_id == schedule_id,
            ContentGenre.is_deleted.is_(False),
        )
    )).all()
    for (gid,) in genre_rows:
        db.add(ContentGenre(
            content_id=target_content_id,
            genre_id=gid,
        ))


async def _inherit_licenses_from(
    db: AsyncSession,
    source_content_id: int,
    target_content_id: int,
) -> None:
    """继承许可证关联：将源内容未删除的许可证关联复制到目标内容（幂等跳过或恢复已存在关联）。

    用于归档产物带入许可证信息，与手动创建子内容继承父类许可证的语义一致：
    - 总季（SEASON）/连续剧（SERIES）继承 SCHEDULE 的许可证
    - 单季（SEASON_SERIES）继承总季（SEASON）的许可证
    - 单集（EPISODE）继承单季的许可证
    """
    source_license_ids = (
        await db.execute(
            select(LicenseContent.license_id).where(
                LicenseContent.content_id == source_content_id,
                LicenseContent.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    if not source_license_ids:
        return

    # 查询目标内容已存在的许可证关联（含软删除），避免唯一约束冲突
    existing_rows = (
        await db.execute(
            select(LicenseContent).where(
                LicenseContent.content_id == target_content_id,
                LicenseContent.license_id.in_(source_license_ids),
            )
        )
    ).scalars().all()
    existing_map = {row.license_id: row for row in existing_rows}

    added = 0
    restored = 0
    for lid in source_license_ids:
        lic = existing_map.get(lid)
        if lic is None:
            db.add(LicenseContent(license_id=lid, content_id=target_content_id))
            added += 1
        elif lic.is_deleted:
            lic.is_deleted = False
            restored += 1
    if added or restored:
        await db.flush()
        logger.info(
            f"归档许可证继承: #{source_content_id} 许可证 {list(source_license_ids)} → #{target_content_id}, 新增 {added} 条, 恢复 {restored} 条"
        )


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


def _build_episode_title(sched_meta: ScheduleMetadata | None, schedule: Content) -> str:
    """生成 EPISODE 标题：Series Name + E+Sequence（如"王者 E03"）。参考创建电视剧内容接口格式。"""
    series_name = sched_meta.series_name if sched_meta else None
    ep_seq = sched_meta.sequence if sched_meta else None
    if series_name and ep_seq is not None:
        return f"{series_name} E{ep_seq:02d}"
    if series_name:
        return series_name
    return schedule.title


async def _copy_schedule_metadata_to_program(
    db: AsyncSession,
    sched_meta: ScheduleMetadata,
    archive_content_id: int,
    archive_content_type: str,
    series_type: int,
) -> None:
    """将 ScheduleMetadata 的元数据复制到 ProgramMetadata。

    仅当 series_type=0（独立节目）时复制完整元数据；
    series_type=1/2 时只复制部分共享字段。
    同时处理 SERIES/SEASON 层级元数据的复制。
    """
    from app.internal.cms_biz_orchestration.models.content_metadata import ContentMetadata, SeriesMetadata
    from app.internal.cms_biz_orchestration.repositories import process_repo
    from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status

    # 元数据由原始节目单带过来，处理人继承原节目单 Metadata 流程记录的处理人；
    # 节目单侧无流程记录时回退到元数据创建人，最终回退 "archive"
    processed_by = "archive"
    source_processes = await process_repo.get_process_by_content_id_and_node_code(
        db, sched_meta.content_id, "Metadata"
    )
    if source_processes:
        latest = max(source_processes, key=lambda p: p.id)
        if latest.assigned:
            processed_by = latest.assigned
    elif sched_meta.created_by:
        processed_by = sched_meta.created_by

    # SeriesType=0: 独立节目，复制完整 ProgramMetadata
    if series_type == 0:
        prog_meta = ContentMetadata(
            content_id=archive_content_id,
            name=sched_meta.name or "",
            vod_type=sched_meta.vod_type,
            type_id=sched_meta.type_id,
            description=sched_meta.description,
            audio_lang=sched_meta.audio_lang,
            subtitle_lang=sched_meta.subtitle_lang,
            rating_level=sched_meta.rating_level,
            advice=sched_meta.advice,
            studio=sched_meta.studio,
            # cdr_id 为每级内容独立的唯一标识，归档产物是 Program 级，按 Program_{id} 生成，
            # 不能继承节目单的 Schedule_{id}；且 sched_meta.cdr_id 可能为 None，会触发 NOT NULL 约束
            cdr_id=f"Program_{archive_content_id}",
            tag_ids=sched_meta.tag_ids,
            status_flag=sched_meta.status_flag,
            sections_info=sched_meta.sections_info,
            created_by=sched_meta.created_by,
        )
        db.add(prog_meta)
        await db.flush()
    else:
        # SeriesType=1/2: 连续剧单集，复制共享字段到 EPISODE 的 ProgramMetadata
        prog_meta = ContentMetadata(
            content_id=archive_content_id,
            name=sched_meta.name or "",
            description=sched_meta.description,
            audio_lang=sched_meta.audio_lang,
            subtitle_lang=sched_meta.subtitle_lang,
            rating_level=sched_meta.rating_level,
            advice=sched_meta.advice,
            studio=sched_meta.studio,
            # 同上：EPISODE 归档产物的 cdr_id 按 Program_{id} 独立生成
            cdr_id=f"Program_{archive_content_id}",
            status_flag=sched_meta.status_flag,
            sections_info=sched_meta.sections_info,
            created_by=sched_meta.created_by,
        )
        db.add(prog_meta)
        await db.flush()

    await db.flush()
    logger.info(f"归档: 复制 ScheduleMetadata id={sched_meta.id} → ProgramMetadata content_id={archive_content_id}")


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
        # SeriesType=1 明确只查 SERIES，避免或查询误匹配到总季下的 SEASON_SERIES 单季
        existing = await _find_content_by_id_or_external_id(
            db, series_id_str, ContentType.SERIES.value
        )

    # ID 未命中 → 名称回退匹配：按 Parent Name 查找已有独立 SERIES（顶层、未删除/未废弃）。
    # 归档导入多行同剧时 series_id 为空，靠名称复用已有连续剧，避免每行新建造成重复
    # （与 VOD 导入按 Parent Name 匹配父类的口径一致）
    if existing is None and series_name:
        existing = (
            await db.execute(
                select(Content).where(
                    Content.title == series_name,
                    Content.content_type == ContentType.SERIES.value,
                    Content.parent_id.is_(None),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                ).limit(1)
            )
        ).scalar_one_or_none()

    if existing:
        return existing

    # 父级分组不打归档标记：归档产物仅叶子(MOVIE/EPISODE)，父级属 VOD 节目单元
    series = Content(
        content_type=ContentType.SERIES.value,
        title=series_name or schedule.title,
        status=ContentStatus.NONE.value,
        series_type=1,
        source_schedule_id=source_schedule_id,
        external_id=series_id_str,
    )
    db.add(series)
    await db.flush()

    # 继承 schedule 的题材到新创建的 SERIES
    await _copy_genre_from_schedule(db, schedule.id, series.id)

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

    # ID 未命中 → 名称回退匹配：按 Show Name 查找已有顶层 SEASON（未删除/未废弃）
    if existing is None and show_name:
        existing = (
            await db.execute(
                select(Content).where(
                    Content.title == show_name,
                    Content.content_type == ContentType.SEASON.value,
                    Content.parent_id.is_(None),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                ).limit(1)
            )
        ).scalar_one_or_none()

    if existing:
        return existing

    # 创建新 SEASON（总季连续剧）
    # 父级分组不打归档标记：归档产物仅叶子(MOVIE/EPISODE)，父级属 VOD 节目单元
    season = Content(
        content_type=ContentType.SEASON.value,
        title=show_name or schedule.title,
        status=ContentStatus.NONE.value,
        series_type=3,
        source_schedule_id=source_schedule_id,
        external_id=show_id_str,
    )
    db.add(season)
    await db.flush()

    # 继承 schedule 的题材到新创建的 SEASON
    await _copy_genre_from_schedule(db, schedule.id, season.id)

    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(db, season.id)

    return season


async def _find_series_by_name_under_season(
    db: AsyncSession,
    series_name: Optional[str],
) -> Optional[Content]:
    """按名称查找总季下的已有单季（含历史 SERIES 类型），用于归档导入多行同剧复用。

    匹配条件：title == 单季名称，且父级为 SEASON（总季），未删除/未废弃。
    单季名称业务上唯一（创建时有季序号唯一性校验），不做季序号匹配，
    避免用户填写的 Series Ordinal 与存储值不一致时误报 Parent not found。
    """
    if not series_name:
        return None

    cond = [
        Content.title == series_name,
        Content.content_type.in_([ContentType.SERIES.value, ContentType.SEASON_SERIES.value]),
        Content.parent_id.is_not(None),
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    ]
    rows = (await db.execute(select(Content).where(*cond).limit(5))).scalars().all()
    for row in rows:
        parent = await db.get(Content, row.parent_id)
        if parent is not None and parent.content_type == ContentType.SEASON.value:
            return row
    return None


async def _find_or_create_series_under_season(
    db: AsyncSession,
    sched_meta: Optional[ScheduleMetadata],
    season: Content,
    schedule: Content,
    source_schedule_id: int,
) -> Content:
    """SeriesType=2: 根据 series_id 在 SEASON 下查找/创建单季连续剧 SEASON_SERIES。

    查找策略：优先按 Content.id（用户选中已有内容时 series_id 存的是 Content.id），
    其次按 Content.external_id 匹配，解决手动创建的内容 external_id 为 NULL 找不到的问题。

    类型说明：总季（SEASON）下的单季类型统一为 SEASON_SERIES（与前端单季下拉、
    总季详情页注入单季一致），查找时明确只匹配 SEASON_SERIES。
    """
    series_name = sched_meta.series_name if sched_meta else None
    series_id_str = sched_meta.series_id if sched_meta else None
    series_ordinal = sched_meta.series_ordinal if sched_meta else None

    existing = None
    if series_id_str:
        existing = await _find_content_by_id_or_external_id(
            db, series_id_str, ContentType.SEASON_SERIES.value
        )

    if existing:
        return existing

    # 创建新单季前校验总季下季序号唯一性，与内容创建时的 SERIES_ORDINAL_EXISTS 校验对齐：
    # 已有 S01 时归档再填季序号 1 应报错，避免产生重复季序号的单季。
    # 注意：此处是冲突检测而非选取查询，两种类型都查，确保历史 SERIES 类型单季也能拦住重复季序号
    if series_ordinal is not None:
        existing_ordinal = (
            await db.execute(
                select(Content.id).where(
                    Content.parent_id == season.id,
                    Content.content_type.in_([ContentType.SERIES.value, ContentType.SEASON_SERIES.value]),
                    Content.series_ordinal == series_ordinal,
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalar_one_or_none()
        if existing_ordinal:
            raise BusinessException(ErrorCode.SERIES_ORDINAL_EXISTS, get_msg("SERIES_ORDINAL_EXISTS"))

    # 创建新 SEASON_SERIES（单季连续剧）
    series = Content(
        content_type=ContentType.SEASON_SERIES.value,
        title=series_name or f"{season.title} S{(series_ordinal or 1):02d}",
        status=ContentStatus.NONE.value,
        parent_id=season.id,
        series_type=2,
        series_ordinal=series_ordinal,
        source_schedule_id=source_schedule_id,
        external_id=series_id_str,
    )
    db.add(series)
    await db.flush()

    # 继承 schedule 的题材到新创建的 SEASON_SERIES
    await _copy_genre_from_schedule(db, schedule.id, series.id)

    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(db, series.id)

    return series


async def _update_volume_count(db: AsyncSession, series_content_id: int) -> None:
    """自动维护 VolumnCount：统计单季（SERIES/SEASON_SERIES）下的 EPISODE 数量并回写。"""
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


async def _build_archive_item(db: AsyncSession, c: Content, meta_name: Optional[str] = None) -> ArchiveListItem:
    """将 ORM Content（VOD 类型）转换为归档列表响应。"""
    genre_ids, genre_name = await _get_genre_names(db, c.id)
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
    elif c.content_type in (ContentType.SERIES.value, ContentType.SEASON.value, ContentType.SEASON_SERIES.value):
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
                channel_parent = (
                    await db.execute(
                        select(Content.title).where(Content.id == schedule_src.parent_id, Content.is_deleted.is_(False))
                    )
                ).scalar_one_or_none()
                channel_name = channel_parent

    return ArchiveListItem(
        id=c.id,
        content_type=c.content_type,
        title=c.title,
        status=c.status,
        genre_ids=genre_ids,
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
        is_discarded=c.is_discarded,
    )


async def list_archives(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    title: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    statuses: Optional[list[str]] = None,
    genre_ids: Optional[list[int]] = None,
    provider_ids: Optional[list[int]] = None,
    package_ids: Optional[list[int]] = None,
    category_id: Optional[int] = None,
    custom_tag_ids: Optional[list[int]] = None,
    deleted: Optional[str] = None,
    type_ids: Optional[list[int]] = None,
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
    publish_date_from: Optional[str] = None,
    publish_date_to: Optional[str] = None,
    unpublish_date_from: Optional[str] = None,
    unpublish_date_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    current_user: Optional[User] = None,
) -> PaginatedResponse[ArchiveListItem]:
    """
    查询归档内容列表（仅 MOVIE/EPISODE/SEASON/SEASON_SERIES/SERIES 类型，分页）。

    输入参数：
        page                页码
        page_size           每页条数（默认 10）
        title               节目名称关键字（模糊）
        content_types       内容类型过滤（限定在归档类型内，默认全选）
        statuses            Ingest 状态列表（任一匹配）
        genre_id            题材 id
        provider_ids        供应商 id 列表（多选，通过 license 链路过滤）
        package_ids         服务包 id 列表（多选，通过 ContentPackage 关联过滤）
        category_id         栏目 id（通过 ContentCategory 关联过滤）
        custom_tag_ids      自定义标签 id 列表（任一匹配；MOVIE/EPISODE 走 program_metadata，SERIES/SEASON 走 series_metadata）
        deleted             是否删除（YES/NO）
        type_ids            类型 id 列表
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
        publish_date_from   发布日期范围下限（通过 PublishTask.publish_time 过滤）
        publish_date_to     发布日期范围上限
        unpublish_date_from 下架日期范围下限（通过 PublishTask.unpublish_time 过滤）
        unpublish_date_to   下架日期范围上限

    输出：
        PaginatedResponse[ArchiveListItem]

    过滤规则：
        - content_type IN (MOVIE/EPISODE/SEASON/SEASON_SERIES/SERIES)
        - is_archived = true（仅归档内容，普通 VOD 内容不在此列）
    """
    logger.info(f"list_archives 入参: page={page}, page_size={page_size}, title={title}, content_types={content_types}, statuses={statuses}, genre_ids={genre_ids}, provider_ids={provider_ids}, package_ids={package_ids}, category_id={category_id}, custom_tag_ids={custom_tag_ids}, deleted={deleted}, type_ids={type_ids}, channel_name={channel_name}, program_name={program_name}, begin_time_from={begin_time_from}, begin_time_to={begin_time_to}, end_time_from={end_time_from}, end_time_to={end_time_to}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, publish_date_from={publish_date_from}, publish_date_to={publish_date_to}, unpublish_date_from={unpublish_date_from}, unpublish_date_to={unpublish_date_to}, sort_by={sort_by}, sort_order={sort_order}")
    # 限制 content_types 只能是归档类型
    allowed = set(_ARCHIVE_TYPES)
    effective_types = [t for t in (content_types or [])] if content_types else _ARCHIVE_TYPES
    effective_types = [t for t in effective_types if t in allowed] or _ARCHIVE_TYPES

    # is_archived 过滤仅作用于全局归档管理视角（不带 source_schedule_id）；
    # 节目单详情归档页签视角需容纳归档时复用的普通 VOD SERIES/SEASON/SEASON_SERIES
    # （复用不打归档标记），其归档语义由下方 source_schedule_id 分支的
    # all_ids（直接归档产物 + 父级链路）限定
    archived_filters = [] if source_schedule_id is not None else [Content.is_archived.is_(True)]

    if deleted == 'YES':
        # 查询已删除的内容（is_discarded=True）
        query = select(Content).where(
            Content.content_type.in_(effective_types),
            Content.is_deleted.is_(False),  # is_deleted 始终为 False
            Content.is_discarded.is_(True),  # 查询已删除的
            *archived_filters,
        )
    else:
        # 查询未删除的内容（is_discarded=False）
        query = select(Content).where(
            Content.content_type.in_(effective_types),
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),  # 查询未删除的
            *archived_filters,
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
            # 父级可能是归档时复用的普通 VOD SERIES/SEASON（未打归档标记），
            # 此处仅校验有效性，不要求 is_archived=True
            valid_parents = set(
                (
                    await db.execute(
                        select(Content.id).where(
                            Content.id.in_(parent_ids),
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
    if genre_ids:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id.in_(genre_ids))
        query = query.where(Content.id.in_(subq))

    if package_ids:
        cids = select(ContentPackage.content_id).where(ContentPackage.package_id.in_(package_ids))
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

    if type_ids:
        content_ids_from_type = set()
        movie_episode_ids = (
            await db.execute(
                select(ContentMetadata.content_id).where(
                    ContentMetadata.type_id.in_(type_ids)
                )
            )
        ).scalars().all()
        content_ids_from_type.update(movie_episode_ids)
        series_season_ids = (
            await db.execute(
                select(SeriesMetadata.content_id).where(
                    SeriesMetadata.type_id.in_(type_ids)
                )
            )
        ).scalars().all()
        content_ids_from_type.update(series_season_ids)
        query = query.where(Content.id.in_(content_ids_from_type))

    # 频道名称过滤：channel_name → source_schedule_id → SCHEDULE → parent_id → CHANNEL
    if channel_name:
        channel_ids = (
            await db.execute(
                select(ChannelMetadata.content_id).where(
                    ChannelMetadata.name.ilike(f"%{channel_name}%"),
                    ChannelMetadata.is_deleted.is_(False),
                    ChannelMetadata.is_discarded.is_(False),
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

    # 节目名称过滤：program_name 匹配归档来源节目单名称（source_schedule_id → SCHEDULE.title）
    if program_name:
        schedule_ids = (
            await db.execute(
                select(Content.id).where(
                    Content.content_type == ContentType.SCHEDULE.value,
                    Content.title.ilike(f"%{program_name}%"),
                    Content.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        if schedule_ids:
            query = query.where(Content.source_schedule_id.in_(schedule_ids))
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
            begin_time_from_str = str(begin_time_from) if not isinstance(begin_time_from, str) else begin_time_from
            dt = datetime.fromisoformat(begin_time_from_str)
            sched_time_query = sched_time_query.where(Content.begin_time >= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        if begin_time_to:
            begin_time_to_str = str(begin_time_to) if not isinstance(begin_time_to, str) else begin_time_to
            dt = datetime.fromisoformat(begin_time_to_str)
            sched_time_query = sched_time_query.where(Content.begin_time <= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        if end_time_from:
            end_time_from_str = str(end_time_from) if not isinstance(end_time_from, str) else end_time_from
            dt = datetime.fromisoformat(end_time_from_str)
            sched_time_query = sched_time_query.where(Content.end_time >= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        if end_time_to:
            end_time_to_str = str(end_time_to) if not isinstance(end_time_to, str) else end_time_to
            dt = datetime.fromisoformat(end_time_to_str)
            sched_time_query = sched_time_query.where(Content.end_time <= dt.replace(tzinfo=app_tz) if dt.tzinfo is None else dt)
        sched_time_ids = (await db.execute(sched_time_query)).scalars().all()
        if sched_time_ids:
            query = query.where(Content.source_schedule_id.in_(sched_time_ids))
        else:
            query = query.where(Content.id == -1)

    if provider_ids:
        contract_ids = (
            await db.execute(
                select(Contract.id).where(Contract.provider_id.in_(provider_ids), Contract.is_deleted.is_(False))
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

        if license_start_from and license_end_to:
            # 两个范围都激活 → 使用 Overlap 语义（start_date <= end_to AND end_date >= start_from）
            query_start = date.fromisoformat(str(license_start_from))
            query_end = date.fromisoformat(str(license_end_to))
            lic_query = lic_query.where(
                License.start_date <= query_end,
                License.end_date >= query_start,
            )
        else:
            # 只有一个范围激活 → 保持原有的逐字段范围过滤
            if license_start_from:
                lic_query = lic_query.where(License.start_date >= date.fromisoformat(str(license_start_from)))
            if license_start_to:
                lic_query = lic_query.where(License.start_date <= date.fromisoformat(str(license_start_to)))
            if license_end_from:
                lic_query = lic_query.where(License.end_date >= date.fromisoformat(str(license_end_from)))
            if license_end_to:
                lic_query = lic_query.where(License.end_date <= date.fromisoformat(str(license_end_to)))

        cids_lic = select(LicenseContent.content_id).where(
            LicenseContent.license_id.in_(lic_query),
            LicenseContent.is_deleted.is_(False),
        )
        query = query.where(Content.id.in_(cids_lic))

    if publish_date_from or publish_date_to:
        pub_query = select(PublishTask.entity_id).where(
            PublishTask.entity_type == 'Content',
            PublishTask.task_type == 'publish',
            PublishTask.status == 'success',
            PublishTask.is_deleted.is_(False),
        )
        if publish_date_from:
            publish_date_from_str = str(publish_date_from) if not isinstance(publish_date_from, str) else publish_date_from
            pub_query = pub_query.where(PublishTask.publish_time >= datetime.fromisoformat(publish_date_from_str).replace(tzinfo=None))
        if publish_date_to:
            publish_date_to_str = str(publish_date_to) if not isinstance(publish_date_to, str) else publish_date_to
            pub_query = pub_query.where(PublishTask.publish_time <= datetime.fromisoformat(publish_date_to_str).replace(hour=23, minute=59, second=59, tzinfo=None))
        query = query.where(Content.id.in_(pub_query))

    if unpublish_date_from or unpublish_date_to:
        unpub_query = select(PublishTask.entity_id).where(
            PublishTask.entity_type == 'Content',
            PublishTask.unpublish_time.is_not(None),
            PublishTask.is_deleted.is_(False),
        )
        if unpublish_date_from:
            unpublish_date_from_str = str(unpublish_date_from) if not isinstance(unpublish_date_from, str) else unpublish_date_from
            unpub_query = unpub_query.where(PublishTask.unpublish_time >= datetime.fromisoformat(unpublish_date_from_str).replace(tzinfo=None))
        if unpublish_date_to:
            unpublish_date_to_str = str(unpublish_date_to) if not isinstance(unpublish_date_to, str) else unpublish_date_to
            unpub_query = unpub_query.where(PublishTask.unpublish_time <= datetime.fromisoformat(unpublish_date_to_str).replace(hour=23, minute=59, second=59, tzinfo=None))
        query = query.where(Content.id.in_(unpub_query))

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
            (Content.content_type.in_([ContentType.SERIES.value, ContentType.SEASON_SERIES.value]), 1),
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

    # 批量查询元数据名称（MOVIE/EPISODE 查 program_metadata，SERIES/SEASON 查 series_metadata）
    content_ids = [c.id for c in rows]
    movie_episode_ids = [c.id for c in rows if c.content_type in (ContentType.MOVIE.value, ContentType.EPISODE.value)]
    series_season_ids = [c.id for c in rows if c.content_type in (ContentType.SERIES.value, ContentType.SEASON.value, ContentType.SEASON_SERIES.value)]

    meta_map: dict[int, str] = {}
    if movie_episode_ids:
        meta_rows = (
            await db.execute(
                select(ContentMetadata.content_id, ContentMetadata.name)
                .where(ContentMetadata.content_id.in_(movie_episode_ids))
            )
        ).all()
        meta_map.update({row.content_id: row.name for row in meta_rows})
    if series_season_ids:
        meta_rows = (
            await db.execute(
                select(SeriesMetadata.content_id, SeriesMetadata.name)
                .where(SeriesMetadata.content_id.in_(series_season_ids))
            )
        ).all()
        meta_map.update({row.content_id: row.name for row in meta_rows})

    items = [await _build_archive_item(db, c, meta_map.get(c.id)) for c in rows]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


# ─── 频道详情 ───────────────────────────────────────────────────────────

async def _build_channel_detail(db: AsyncSession, c: Content) -> ChannelDetailItem:
    """将 ORM Content（CHANNEL 类型）转换为频道详情响应。"""
    genre_ids, genre_name = await _get_genre_names(db, c.id)
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

    # 自定义标签名称
    custom_tag_names: list[str] = []
    ct_rows = (
        await db.execute(
            select(CustomTag.name)
            .join(ContentCustomTag, ContentCustomTag.custom_tag_id == CustomTag.id)
            .where(ContentCustomTag.content_id == c.id, CustomTag.is_deleted.is_(False))
        )
    ).scalars().all()
    custom_tag_names = list(ct_rows)

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

    # 查询元数据名称
    meta_name = (
        await db.execute(
            select(ChannelMetadata.name).where(
                ChannelMetadata.content_id == c.id,
                ChannelMetadata.is_deleted.is_(False),
                ChannelMetadata.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    return ChannelDetailItem(
        id=c.id,
        title=c.title,
        content_type=c.content_type,
        status=c.status,
        genre_ids=genre_ids,
        genre_name=genre_name,
        package_names=pkg_names,
        category_names=category_names,
        custom_tag_names=custom_tag_names,
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


async def update_channel(
    db: AsyncSession, channel_id: int, data: ChannelUpdate, processed_by: str | None = None
) -> ChannelDetailItem:
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
    if data.genre_ids is not None:
        # 删除现有题材关联
        await db.execute(
            ContentGenre.__table__.delete().where(ContentGenre.content_id == channel_id)
        )
        # 添加新的题材关联
        for gid in data.genre_ids:
            db.add(ContentGenre(content_id=channel_id, genre_id=gid))

    # 编辑频道基础信息属于节点数据变更，回退内容状态（已发布/准备发布等 → InProgress）
    await rollback_after_published_edit(
        db, channel_id, channel.content_type, processed_by or "system", "编辑频道基础信息"
    )

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
    # 校验业务频道存在（允许查看已废弃频道的物理频道列表，用于审计和恢复）
    channel = (
        await db.execute(
            select(Content.id).where(
                Content.id == channel_id,
                Content.content_type == "CHANNEL",
                Content.is_deleted.is_(False),
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

    # 查询字典码值→名称映射
    from app.internal.cms_biz_system.models.dict import DictNode

    async def _load_dict_name_map(parent_code: str) -> dict[str, str]:
        parent = (
            await db.execute(select(DictNode).where(DictNode.code == parent_code, DictNode.is_deleted.is_(False)))
        ).scalar_one_or_none()
        if not parent:
            return {}
        children = (
            await db.execute(
                select(DictNode).where(DictNode.parent_id == parent.id, DictNode.is_deleted.is_(False))
            )
        ).scalars().all()
        return {c.code: c.name for c in children}

    mediaservice_name_map = await _load_dict_name_map("mediaservice")
    definition_name_map = await _load_dict_name_map("Definition")
    videoencode_name_map = await _load_dict_name_map("Videoencode")

    items = [
        PhysicalChannelListItem(
            id=pc.id,
            channel_id=pc.channel_id,
            name=pc.name,
            channel_number=pc.channel_number,
            status=pc.status,
            mediaservice=pc.mediaservice,
            mediaservice_name=mediaservice_name_map.get(pc.mediaservice) if pc.mediaservice else None,
            definition=pc.definition,
            definition_name=definition_name_map.get(pc.definition) if pc.definition else None,
            videoencode=pc.videoencode,
            videoencode_name=videoencode_name_map.get(pc.videoencode) if pc.videoencode else None,
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
        # 已发布/已下架内容编辑后回滚状态并新建提交审核记录（与 Metadata 编辑行为一致）
        await rollback_after_published_edit(
            db,
            content_id=channel_id,
            content_type=channel_content.content_type,
            edited_by=processed_by or "system",
            edit_info="添加物理频道",
        )
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

    # 先标记删除，确保后续查询不会包含此记录
    pc.is_deleted = True

    # 任意物理频道删除都需触发状态回滚（与 Metadata 编辑行为一致）；
    # 若删除后频道下无物理频道，complete_process 内部会通过 should_be_waiting_for_materials
    # 将状态置为 WaitingForMaterials（缺少素材），否则置为 InProgress（处理中）
    channel_content = (
        await db.execute(
            select(Content).where(Content.id == channel_id)
        )
    ).scalar_one_or_none()
    if channel_content:
        # 已发布/已下架内容编辑后回滚状态并新建提交审核记录
        await rollback_after_published_edit(
            db,
            content_id=channel_id,
            content_type=channel_content.content_type,
            edited_by=processed_by or "system",
            edit_info="删除物理频道",
        )
        await complete_process_and_update_status(
            db,
            content_id=channel_id,
            content_type=channel_content.content_type,
            process_name="InjectSubContent",
            processed_by=processed_by,
            info=f"删除物理频道: {pc.name}",
        )

    await db.commit()


async def list_physical_channel_history(
    db: AsyncSession,
    channel_id: int,
    page: int = 1,
    page_size: int = 10,
    processed_type: str | None = None,
    processed_by: str | None = None,
    processed_at_from: str | None = None,
    processed_at_to: str | None = None,
) -> PaginatedResponse[PhysicalChannelHistoryItem]:
    """
    查询物理频道操作历史记录。
    只查询未被废弃的物理频道的历史记录（关联 PhysicalChannel 表过滤 is_discarded=False）。

    输入：channel_id, page, page_size, processed_type, processed_by, processed_at_from, processed_at_to
    输出：PaginatedResponse[PhysicalChannelHistoryItem]
    """
    logger.info(f"list_physical_channel_history 入参: channel_id={channel_id}, page={page}, page_size={page_size}, processed_type={processed_type}, processed_by={processed_by}, processed_at_from={processed_at_from}, processed_at_to={processed_at_to}")

    query = select(PhysicalChannelHistory).join(
        PhysicalChannel,
        PhysicalChannelHistory.physical_channel_id == PhysicalChannel.id,
        isouter=True
    ).where(
        PhysicalChannelHistory.channel_id == channel_id,
        PhysicalChannel.is_discarded.is_(False),
    )

    if processed_type:
        query = query.where(PhysicalChannelHistory.processed_type == processed_type)
    if processed_by:
        query = query.where(PhysicalChannelHistory.processed_by.ilike(f"%{processed_by}%"))
    if processed_at_from:
        query = query.where(func.timezone('Asia/Shanghai', PhysicalChannelHistory.processed_at) >= func.to_timestamp(processed_at_from, 'YYYY-MM-DD HH24:MI:SS'))
    if processed_at_to:
        query = query.where(func.timezone('Asia/Shanghai', PhysicalChannelHistory.processed_at) <= func.to_timestamp(processed_at_to, 'YYYY-MM-DD HH24:MI:SS'))

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
            # 已发布/已下架内容编辑后回滚状态并新建提交审核记录（与 Metadata 编辑行为一致）
            await rollback_after_published_edit(
                db,
                content_id=content_id,
                content_type=content.content_type,
                edited_by=processed_by or "system",
                edit_info="关联服务包",
            )
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


async def unlink_content_package(
    db: AsyncSession, content_id: int, package_id: int, processed_by: str | None = None
) -> None:
    """
    解除内容与服务包的关联。

    输入参数：
        content_id    内容 id
        package_id    服务包 id
        processed_by  操作人用户名（用于流程记录）
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

    # 写入一条 info 以"删除"开头的 Package 流程分界记录（与 package_service.remove_content_from_package
    # 同一口径）：1) Processes 页签可见取消关联操作；2) 隔断更早的 Passed 历史，
    # 保证之后再次关联时 Processed Before 判定为首次处理（红色）
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
            info=f"删除服务包关联: package_id={package_id}",
        )
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
                raise BusinessException(
                    ErrorCode.CATEGORY_CONTENT_LIMIT_EXCEEDED,
                    get_msg("CATEGORY_CONTENT_LIMIT_EXCEEDED", name=cat.name, limit=cat.vod_count)
                )
        
        # 查询当前栏目下最大 sequence
        max_seq = (
            await db.execute(
                select(func.coalesce(func.max(ContentCategory.sequence), 0)).where(
                    ContentCategory.category_id == cid,
                    ContentCategory.is_deleted.is_(False),
                    ContentCategory.is_discarded.is_(False),
                )
            )
        ).scalar_one()

        link = ContentCategory(content_id=content_id, category_id=cid, sequence=max_seq + 1)
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
            # 已发布/已下架内容编辑后回滚状态并新建提交审核记录（与 Metadata 编辑行为一致）
            await rollback_after_published_edit(
                db,
                content_id=content_id,
                content_type=content.content_type,
                edited_by=processed_by or "system",
                edit_info="关联栏目",
            )
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
    from app.internal.cms_biz_system.services.operation_log_service import translate_log_value
    from app.internal.cms_biz_system.models.user import User

    logs = (
        await db.execute(
            select(OperationLog).where(
                OperationLog.content_id == content_id
            ).order_by(OperationLog.id.desc()).limit(50)
        )
    ).scalars().all()

    user_ids = {log.user_id for log in logs if log.user_id}
    display_name_map: dict[int, str] = {}
    if user_ids:
        u_result = await db.execute(
            select(User.id, User.display_name).where(User.id.in_(user_ids))
        )
        display_name_map = {uid: dn for uid, dn in u_result.all() if dn}

    items = [
        ActivityLogListItem(
            id=log.id,
            processed_at=log.operation_time,
            processed_by=log.user_name,
            processed_by_display_name=display_name_map.get(log.user_id) if log.user_id else None,
            processed_type=log.operation_type,
            details=translate_log_value(log.operation_content),
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
    # 使用 ORDER BY LicenseContent.id DESC 确保返回最新绑定的许可证
    # （内容可能关联多个许可证，需使用最新绑定的供应商审批配置）
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
            .order_by(LicenseContent.id.desc())
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

    # 查询供应商的审批层级（与上面查询保持一致，使用最新的许可证关联）
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
            .order_by(LicenseContent.id.desc())
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
                PublishTask.is_deleted.is_(False),
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
    
    application_review = ContentProcess(
        content_id=content_id,
        name="ApplicationReview",
        node_code="ApplicationReview",
        sequence=3,
        start_dt=now,
        end_dt=now,
        status="Passed",
        assigned=initiated_by,
        info=get_msg("PROCESS_APPLICATION_REVIEW_INITIATED", by=initiated_by),
    )
    db.add(application_review)

    # 非免审批且内容已处于发布流程及之后状态：重新发起审核需重置为 InProgress，
    # 使 ContentReview / PublishPlan 节点回到未完成状态
    RESET_REVIEW_STATUSES = {"ReadyForPublish", "Publishing", "PublishFailed", "Published", "NoActiveLicense", "Closed"}
    if level_required > 0 and content.status in RESET_REVIEW_STATUSES:
        old_status = content.status
        content.status = "InProgress"
        from app.internal.cms_biz_orchestration.services.workflow_service import record_status_change
        await record_status_change(db, content_id, old_status, "InProgress", initiated_by)
        logger.info(f"重新发起审核，重置内容状态 | content_id={content_id} old_status={old_status} new_status=InProgress")
    
    # 免审批：直接通过
    if level_required == 0:
        # 软删除已有的 ContentReview 记录（如从 L3 审批变更为免审时，旧记录需清理）
        old_review = (
            await db.execute(
                select(ContentReview)
                .where(
                    ContentReview.content_id == content_id,
                    ContentReview.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        for r in old_review:
            r.is_deleted = True
        if old_review:
            logger.info(f"免审提交，软删除旧 ContentReview 记录 | content_id={content_id} count={len(old_review)}")

        review_process = ContentProcess(
            content_id=content_id,
            name="ContentReview",
            node_code="ContentReview",
            sequence=4,
            start_dt=now,
            status="Passed",
            end_dt=now,
            info=get_msg("PROCESS_REVIEW_AUTO_APPROVED"),
        )
        db.add(review_process)
        
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
            "message": get_msg("MSG_REVIEW_AUTO_APPROVED"),
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
            # 之前被拒绝，复用现有审批记录，重置级别状态（保留 level_*_by 历史）
            existing_review.final_status = "Pending"
            existing_review.level_1_status = "Pending"
            existing_review.level_2_status = "Pending"
            existing_review.level_3_status = "Pending"
            existing_review.level_1_at = None
            existing_review.level_2_at = None
            existing_review.level_3_at = None
            existing_review.completed_at = None
            existing_review.initiated_by = initiated_by
            existing_review.initiated_at = now
            logger.info(f"重新发起审核（复用内容审核记录）| content_id={content_id} review_id={existing_review.id}")

            # 复用 L1 任务（重置为 Pending），L2/L3 任务保留不变（后续由 submit_content_review 恢复）
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
                if task.task_type == "review L1":
                    task.task_status = "Pending"
                    task.end_time = None
                    task.start_time = now  # 重置为本轮重新提交时间（原值停留在首次提交）
                    if l1_assignee_id:
                        task.assignee_id = l1_assignee_id
                    logger.info(f"复用 L1 审批任务，重置为待处理 | content_id={content_id} task_id={task.id}")
                else:
                    logger.info(f"保留 L2/L3 审批任务，等待上一级通过后恢复 | content_id={content_id} task_id={task.id} task_type={task.task_type}")
            
            # 为 L1 审批人添加数据权限
            if l1_assignee_id:
                await _ensure_content_auth(db, content_id, l1_assignee_id)
            
            # 查询 L1 审批人用户名
            l1_assignee_name = None
            if l1_assignee_id:
                l1_user = (await db.execute(select(User).where(User.id == l1_assignee_id, User.is_deleted.is_(False)))).scalar_one_or_none()
                l1_assignee_name = l1_user.username if l1_user else None
            
            # 创建新的 L1 ContentProcess（保留旧的历史流程记录不删除）
            l1_process = ContentProcess(
                content_id=content_id,
                name="ContentReview",
                node_code="ContentReview",
                sequence=4,
                start_dt=now,
                status="Pending",
                assigned=l1_assignee_name,
                info=get_msg("PROCESS_REVIEW_PENDING", level="L1", assignee=l1_assignee_name or get_msg("NOT_FOUND")),
            )
            db.add(l1_process)
            
            # 更新 arrangement 任务状态：仅在内容已发布或已下架时标记为完成
            # 申请审核时内容状态为 InProgress，arrangement 任务保持 Pending
            arrangement_task = (
                await db.execute(
                    select(Task).where(
                        Task.content_id == content_id,
                        Task.task_type == "arrangement",
                        Task.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if arrangement_task and content.status in ("Published", "Closed"):
                if arrangement_task.task_status != "Completed":
                    old_status = arrangement_task.task_status
                    arrangement_task.task_status = "Completed"
                    arrangement_task.end_time = now
                    
                    # 记录 arrangement 任务完成日志
                    from app.internal.cms_biz_package.models.task import TaskHistory
                    db.add(
                        TaskHistory(
                            task_id=arrangement_task.id,
                            processed_type="Complete",
                            processed_by=initiated_by,
                            previous_value=old_status,
                            updated_value="任务完成: 内容已发布或已下架",
                        )
                    )
                    logger.info(f"arrangement任务完成 | content_id={content_id} content_status={content.status} task_id={arrangement_task.id}")
            elif arrangement_task:
                logger.info(f"arrangement任务保持Pending | content_id={content_id} content_status={content.status} task_id={arrangement_task.id}")
            
            logger.info(f"重新发起审核完成 | content_id={content_id} level_required={level_required}")
            
            return {
                "success": True,
                "content_id": content_id,
                "review_status": "pending",
                "message": get_msg("MSG_REVIEW_REINITIATED", level_required=level_required),
                "auto_approved": False,
                "level_required": level_required,
            }
        # 如果是 Passed 状态，软删除旧记录后创建新的审批记录（重新审核流程）
        if existing_review and existing_review.final_status == "Passed":
            existing_review.is_deleted = True
            # 软删除旧的 review L1/L2/L3 任务，避免与新任务冲突
            await db.execute(
                update(Task)
                .where(
                    Task.content_id == content_id,
                    Task.task_type.in_(["review L1", "review L2", "review L3"]),
                    Task.is_deleted == False,
                )
                .values(is_deleted=True)
            )
            logger.info(f"软删除旧 Passed 审批记录和任务 | content_id={content_id} review_id={existing_review.id}")

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

    # 更新 arrangement 任务状态：仅在内容已发布或已下架时标记为完成
    # 申请审核时内容状态为 InProgress，arrangement 任务保持 Pending
    arrangement_task = (
        await db.execute(
            select(Task).where(
                Task.content_id == content_id,
                Task.task_type == "arrangement",
                Task.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if arrangement_task and content.status in ("Published", "Closed"):
        if arrangement_task.task_status != "Completed":
            old_status = arrangement_task.task_status
            arrangement_task.task_status = "Completed"
            arrangement_task.end_time = now
            
            # 记录 arrangement 任务完成日志
            from app.internal.cms_biz_package.models.task import TaskHistory
            db.add(
                TaskHistory(
                    task_id=arrangement_task.id,
                    processed_type="Complete",
                    processed_by=initiated_by,
                    previous_value=old_status,
                    updated_value="任务完成: 内容已发布或已下架",
                )
            )
            logger.info(f"arrangement任务完成 | content_id={content_id} content_status={content.status} task_id={arrangement_task.id}")
    elif arrangement_task:
        if arrangement_task.task_status == "Completed":
            # 重新提交审核（内容从 Published/ReadyForPublish 等回退到 InProgress）时，
            # arrangement 任务需恢复为待处理，并重置本轮处理周期时间
            old_status = arrangement_task.task_status
            arrangement_task.task_status = "Pending"
            arrangement_task.end_time = None
            arrangement_task.start_time = now

            # 记录 arrangement 任务恢复日志
            from app.internal.cms_biz_package.models.task import TaskHistory
            db.add(
                TaskHistory(
                    task_id=arrangement_task.id,
                    processed_type="Reopen",
                    processed_by=initiated_by,
                    previous_value=old_status,
                    updated_value="任务恢复待处理: 重新提交审核",
                )
            )
            logger.info(f"arrangement任务恢复待处理 | content_id={content_id} task_id={arrangement_task.id}")
        else:
            logger.info(f"arrangement任务保持Pending | content_id={content_id} content_status={content.status} task_id={arrangement_task.id}")
    
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
        info=get_msg("PROCESS_REVIEW_PENDING", level="L1", assignee=l1_assignee_name or get_msg("NOT_FOUND")),
    )
    db.add(l1_process)
    
    # L2/L3 任务在上一级审批通过后创建，这里不创建
    logger.info(f"创建L1审批任务，等待审批 | content_id={content_id} level_required={level_required}")
    
    return {
        "success": True,
        "content_id": content_id,
        "review_status": "pending",
        "message": get_msg("MSG_REVIEW_INITIATED", level_required=level_required),
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
    
    # 更新对应级别的审批状态（每个级别只更新自己的字段，不覆盖已完成的级别）
    if level_num == 1:
        content_review.level_1_status = "Passed" if review_type == "approve" else "Rejected"
        content_review.level_1_by = processed_by
        content_review.level_1_at = now
        content_review.level_1_comment = description
    
    if level_num == 2:
        content_review.level_2_status = "Passed" if review_type == "approve" else "Rejected"
        content_review.level_2_by = processed_by
        content_review.level_2_at = now
        content_review.level_2_comment = description
    
    if level_num == 3:
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
    
    # 查询当前级别的任务（包含Pending和未分配状态）
    current_task = (
        await db.execute(
            select(Task).where(
                Task.content_id == content_id,
                Task.task_type == f"review {current_level}",
                Task.task_status.in_(["Pending", "Not Assigned"]),
                Task.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()

    # 非 ADMIN 用户需要权限校验
    if not await is_admin_user(db, processed_by):
        if not current_task:
            raise NotFoundException(ErrorCode.REVIEW_TASK_NOT_FOUND, get_msg("REVIEW_TASK_NOT_FOUND"))

        if current_task.task_status == "Not Assigned":
            raise ForbiddenException(ErrorCode.REVIEW_NO_PERMISSION, get_msg("REVIEW_NO_PERMISSION"))

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
            ).order_by(ContentProcess.created_at.desc()).limit(1)
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
            current_process.info = get_msg("PROCESS_REVIEW_APPROVED", level=current_level, by=processed_by)
        
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
            "message": get_msg("MSG_REVIEW_APPROVED"),
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
            current_process.info = get_msg("PROCESS_REVIEW_REJECTED", level=current_level, by=processed_by, reason=description or "")

        # 审核拒绝后重置 ApplicationReview 节点状态：
        # 将该内容所有 ApplicationReview 的 Passed 记录标记为 Failed，
        # 使前端"提交审核"按钮由绿勾变回红叉；重新提交审核时会创建新的 Passed 记录
        app_review_processes = (await db.execute(
            select(ContentProcess).where(
                ContentProcess.content_id == content_id,
                ContentProcess.node_code == "ApplicationReview",
                ContentProcess.status == "Passed",
                ContentProcess.is_deleted.is_(False),
            )
        )).scalars().all()
        for app_review in app_review_processes:
            app_review.status = "Failed"
            app_review.end_dt = now
            app_review.assigned = processed_by
            app_review.info = get_msg("PROCESS_REVIEW_REJECTED", level=current_level, by=processed_by, reason=description or "")
        if app_review_processes:
            logger.info(f"审核拒绝，重置 ApplicationReview 节点状态 | content_id={content_id} count={len(app_review_processes)}")
        
        # 拒绝后不更新 content.status（拒绝≠准备发布），只恢复 arrangement 任务
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
            
            # 记录 arrangement 任务恢复日志
            from app.internal.cms_biz_package.models.task import TaskHistory
            db.add(
                TaskHistory(
                    task_id=arrangement_task.id,
                    processed_type="Reopen",
                    processed_by=processed_by,
                    previous_value="Completed",
                    updated_value=f"任务恢复待处理: 审核拒绝 - {description or ''}",
                )
            )
        
        logger.info(f"内容审核不通过 | content_id={content_id} by={processed_by} issues={issue_types}")
        
        return {
            "success": True,
            "content_id": content_id,
            "review_status": "rejected",
            "message": get_msg("MSG_REVIEW_REJECTED"),
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
            
            # 查找已有下一级任务（复用现有任务以保持历史连贯性），不存在则新建
            next_task = (
                await db.execute(
                    select(Task).where(
                        Task.content_id == content_id,
                        Task.task_type == f"review {next_level}",
                        Task.is_deleted.is_(False),
                    )
                )
            ).scalar_one_or_none()
            
            if next_task:
                # 复用现有任务，开始时间重置为本轮恢复时间（保证本轮处理周期时间正确）
                next_task.task_status = "Pending" if next_assignee_id else "Not Assigned"
                next_task.end_time = None
                next_task.start_time = now
                if next_assignee_id:
                    next_task.assignee_id = next_assignee_id
                logger.info(f"复用 {next_level} 审批任务，恢复为待处理 | content_id={content_id} task_id={next_task.id}")
            else:
                # 新建任务
                next_task = Task(
                    content_id=content_id,
                    task_type=f"review {next_level}",
                    assignee_id=next_assignee_id,
                    task_status="Pending" if next_assignee_id else "Not Assigned",
                    start_time=now,
                    end_time=None,
                )
                db.add(next_task)
                logger.info(f"创建 {next_level} 审批任务 | content_id={content_id}")
            
            await db.flush()
            
            # 为下一级审批人添加数据权限
            if next_assignee_id:
                await _ensure_content_auth(db, content_id, next_assignee_id)
            
            # 查询下一级审批人名称
            next_assignee_name = None
            if next_assignee_id:
                next_user = (await db.execute(select(User).where(User.id == next_assignee_id, User.is_deleted.is_(False)))).scalar_one_or_none()
                next_assignee_name = next_user.username if next_user else None
            
            # 创建下一级流程记录（首次创建）
            next_process = ContentProcess(
                content_id=content_id,
                name="ContentReview",
                node_code="ContentReview",
                sequence=4,
                start_dt=now,
                status="Pending",
                assigned=next_assignee_name,
                info=get_msg("PROCESS_REVIEW_PENDING", level=next_level, assignee=next_assignee_name or get_msg("NOT_FOUND")),
            )
            db.add(next_process)
            
            logger.info(f"{current_level}审批通过，{next_level}任务已就绪 | content_id={content_id} assignee={next_assignee_name}")
        
        logger.info(f"内容审核部分通过，等待下一级 | content_id={content_id} current_level={current_level}")
        
        return {
            "success": True,
            "content_id": content_id,
            "review_status": "pending",
            "message": get_msg("MSG_REVIEW_LEVEL_APPROVED_WAITING", level=current_level),
            "final_approved": False,
        }


async def get_content_review_status(db: AsyncSession, content_id: int) -> Optional[dict]:
    """获取内容审核状态。"""
    from app.internal.cms_biz_orchestration.models.content_review import ContentReview
    from app.internal.cms_biz_orchestration.models.content_process import ContentProcess

    # 按当前许可证的供应商审批配置判断是否需要审核
    # 若当前供应商为免审，则旧的人工审批记录不再作为有效审核状态展示
    review_info = await _get_content_provider_review_info(db, content_id)
    if review_info["level_required"] == 0:
        auto_approved_process = (
            await db.execute(
                select(ContentProcess)
                .where(
                    ContentProcess.content_id == content_id,
                    ContentProcess.node_code == "ContentReview",
                    ContentProcess.status == "Passed",
                    ContentProcess.is_deleted.is_(False),
                )
                .order_by(ContentProcess.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if auto_approved_process:
            # 免审自动通过场景，返回特殊状态以区分"未提交审核"
            return {
                "id": auto_approved_process.id,
                "content_id": content_id,
                "review_level": "None",
                "level_required": 0,
                "level_1_status": "Passed",
                "level_1_by": None,
                "level_1_at": auto_approved_process.end_dt,
                "level_2_status": "None",
                "level_2_by": None,
                "level_2_at": None,
                "level_3_status": "None",
                "level_3_by": None,
                "level_3_at": None,
                "final_status": "AutoApproved",
                "initiated_by": auto_approved_process.assigned,
                "initiated_at": auto_approved_process.start_dt,
                "completed_at": auto_approved_process.end_dt,
            }
        return None

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
    ).scalars().first()
    
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


# ─── 归档管理 Excel 导出/导入 ─────────────────────────────────────────────

# Keywords 字段双下拉编码 → 名称映射（与前端 MetadataModal.tsx 硬编码选项保持一致）
# 数组结构：[exclusive_value, hdr_value]
_KEYWORDS_EXCLUSIVE_MAP = {"0": "Non-platform exclusive", "1": "Only Tivibu"}
_KEYWORDS_HDR_MAP = {"0": "Non-HDR content", "1": "HDR content"}


def _format_keywords_export(keywords_arr: list[str] | None) -> str:
    """将 keywords 数组（编码）转为可读名称，格式：'Exclusive: <name>, HDR: <name>'。"""
    if not keywords_arr:
        return ""
    parts: list[str] = []
    if len(keywords_arr) >= 1 and keywords_arr[0] is not None and keywords_arr[0] != "":
        name = _KEYWORDS_EXCLUSIVE_MAP.get(str(keywords_arr[0]))
        if name:
            parts.append(f"Exclusive: {name}")
    if len(keywords_arr) >= 2 and keywords_arr[1] is not None and keywords_arr[1] != "":
        name = _KEYWORDS_HDR_MAP.get(str(keywords_arr[1]))
        if name:
            parts.append(f"HDR: {name}")
    return ", ".join(parts)


# 归档导出表头：与 VOD 导出 47 列结构对齐，另加 Program Name（来源节目单名称），共 46 列
# 注意：导出文件仅供查看/对账，不可直接再导入（导入模板为 33 列）；
# Series Type 已从导出移除（统一以 Content Type 区分）；RatingType/RatingId 已移除（评分来源不导出）。
ARCHIVE_EXPORT_HEADERS = [
    # Content 主表（6）
    "Content ID", "Content Type", "Content Name(*)", "Parent Name",
    "Series Ordinal", "Sequence",
    # 主表扩展（3）：Ingest Status / Is Discarded
    "Ingest Status", "Is Discarded",
    # 归档特有（4）：来源节目单信息（比 VOD 导出多 Program Name）
    "Channel Name", "Program Name", "Begin Time", "End Time",
    # 关联（6）：Genre/Type/Tags/Custom Tags/Category/Package
    "Genre(*)", "Type(*)", "Tags", "Custom Tags", "Category", "Package",
    # 版权/发布（5）：Provider / License / Publish
    "Provider", "License Start", "License End", "Unpublish Date", "Publish Date",
    # 元数据-字典（4）
    "VodType(*)", "Language", "RatingLevel(*)", "Advice",
    # 元数据-名称（5）
    "SortName", "OriginalName", "OriginalCountry", "ShortTitle", "ReleaseYear",
    # 元数据-描述（2）
    "Description", "Studio",
    # 元数据-评分（1）：RatingType/RatingId 已移除
    "Rating",
    # 元数据-音视频（4）
    "AudioLang", "SubtitleLang", "BeginDuration", "EndDuration",
    # 元数据-标识（4）
    "Keywords", "Metalayout(*)", "StatusFlag", "SeriesFlag",
    # 元数据-章节（1）
    "SectionsInfo",
    # 审计（2）
    "Created At", "Updated At",
]


async def export_archives_excel(db: AsyncSession, ids: list[int]) -> bytes:
    """导出归档内容为 Excel 文件（46 列，与 VOD 导出对齐 + Program Name）。"""
    import openpyxl

    # 查询归档内容（仅 MOVIE/EPISODE/SEASON/SERIES/SEASON_SERIES 类型）
    rows = (
        await db.execute(
            select(Content).where(
                Content.id.in_(ids),
                Content.content_type.in_([
                    ContentType.MOVIE.value,
                    ContentType.EPISODE.value,
                    ContentType.SEASON.value,
                    ContentType.SERIES.value,
                    ContentType.SEASON_SERIES.value,
                ]),
                Content.is_deleted.is_(False),
            )
        )
    ).scalars().all()

    # 批量查询父级内容标题
    parent_ids = {r.parent_id for r in rows if r.parent_id is not None}
    parent_name_map: dict[int, str] = {}
    if parent_ids:
        parent_rows = (
            await db.execute(
                select(Content.id, Content.title).where(Content.id.in_(parent_ids))
            )
        ).all()
        parent_name_map = {pid: title for pid, title in parent_rows}

    # 批量查询归档特有字段：Channel Name / Begin Time / End Time
    # 来源链：content.source_schedule_id → schedule Content → schedule.begin_time/end_time
    #         schedule.parent_id → channel Content.title
    schedule_ids = {r.source_schedule_id for r in rows if r.source_schedule_id is not None}
    schedule_map: dict[int, Content] = {}
    channel_parent_ids: set[int] = set()
    if schedule_ids:
        schedule_rows = (
            await db.execute(
                select(Content).where(
                    Content.id.in_(schedule_ids),
                    Content.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        schedule_map = {s.id: s for s in schedule_rows}
        channel_parent_ids = {s.parent_id for s in schedule_rows if s.parent_id is not None}

    channel_name_map: dict[int, str] = {}
    if channel_parent_ids:
        channel_rows = (
            await db.execute(
                select(Content.id, Content.title).where(
                    Content.id.in_(channel_parent_ids),
                    Content.is_deleted.is_(False),
                )
            )
        ).all()
        channel_name_map = {cid: title for cid, title in channel_rows}

    # 批量查询来源节目单的元数据（ScheduleMetadata），用于获取 series_ordinal
    # 归档产物（MOVIE/EPISODE）自身的 Content.series_ordinal 为空，需从来源节目单取
    # （Series Type 已从导出移除，不再需要 series_type）
    schedule_meta_by_schedule_id: dict[int, ScheduleMetadata] = {}
    if schedule_ids:
        sched_meta_rows = (
            await db.execute(
                select(ScheduleMetadata).where(
                    ScheduleMetadata.content_id.in_(list(schedule_ids)),
                    ScheduleMetadata.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        schedule_meta_by_schedule_id = {m.content_id: m for m in sched_meta_rows}

    # 批量查询关联数据
    content_ids = [r.id for r in rows]
    movie_type_ids = {ContentType.MOVIE.value, ContentType.EPISODE.value}
    series_type_ids = {ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.SEASON.value}

    # Genre
    genre_rows = (
        await db.execute(
            select(ContentGenre.content_id, Genre.name)
            .join(Genre, ContentGenre.genre_id == Genre.id)
            .where(ContentGenre.content_id.in_(content_ids))
        )
    ).all()
    genre_map: dict[int, list[str]] = {}
    for cid, gname in genre_rows:
        genre_map.setdefault(cid, []).append(gname)

    # Type
    type_ids_set: set[int] = set()
    program_meta_map: dict[int, ContentMetadata] = {}
    series_meta_map: dict[int, SeriesMetadata] = {}
    if content_ids:
        program_metas = (
            await db.execute(
                select(ContentMetadata).where(
                    ContentMetadata.content_id.in_(content_ids),
                    ContentMetadata.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        program_meta_map = {m.content_id: m for m in program_metas}
        for m in program_metas:
            if m.type_id:
                type_ids_set.add(m.type_id)

        series_metas = (
            await db.execute(
                select(SeriesMetadata).where(
                    SeriesMetadata.content_id.in_(content_ids),
                    SeriesMetadata.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        series_meta_map = {m.content_id: m for m in series_metas}
        for m in series_metas:
            if m.type_id:
                type_ids_set.add(m.type_id)

    type_name_map: dict[int, str] = {}
    if type_ids_set:
        type_rows = (
            await db.execute(select(ContentTypeModel.id, ContentTypeModel.name).where(ContentTypeModel.id.in_(type_ids_set)))
        ).all()
        type_name_map = {tid: name for tid, name in type_rows}

    # Tags（元数据中的 tag_ids，从 ContentMetadata/SeriesMetadata 表获取 tag_id，再通过 Tag 表获取名称）
    all_tag_ids: set[int] = set()
    for meta in list(program_meta_map.values()) + list(series_meta_map.values()):
        if meta.tag_ids:
            all_tag_ids.update(meta.tag_ids)
    tag_name_map: dict[int, str] = {}
    if all_tag_ids:
        tag_rows = (
            await db.execute(select(Tag.id, Tag.name).where(Tag.id.in_(list(all_tag_ids))))
        ).all()
        tag_name_map = {tid: tname for tid, tname in tag_rows}
    # 构建 content_id -> tag_names 映射
    tags_map: dict[int, list[str]] = {cid: [] for cid in content_ids}
    for meta in list(program_meta_map.values()) + list(series_meta_map.values()):
        if meta.tag_ids and meta.content_id in tags_map:
            tags_map[meta.content_id] = [tag_name_map.get(tid, "") for tid in meta.tag_ids if tag_name_map.get(tid)]

    # Custom Tags（从 ContentCustomTag 中间表获取）
    custom_tag_rows = (
        await db.execute(
            select(ContentCustomTag.content_id, CustomTag.name)
            .join(CustomTag, ContentCustomTag.custom_tag_id == CustomTag.id)
            .where(ContentCustomTag.content_id.in_(content_ids))
        )
    ).all()
    custom_tag_map: dict[int, list[str]] = {}
    for cid, tname in custom_tag_rows:
        custom_tag_map.setdefault(cid, []).append(tname)

    # Category
    cat_rows = (
        await db.execute(
            select(ContentCategory.content_id, Category.name)
            .join(Category, ContentCategory.category_id == Category.id)
            .where(ContentCategory.content_id.in_(content_ids))
        )
    ).all()
    cat_map: dict[int, list[str]] = {}
    for cid, cname in cat_rows:
        cat_map.setdefault(cid, []).append(cname)

    # Package
    pkg_rows = (
        await db.execute(
            select(ContentPackage.content_id, Package.name)
            .join(Package, ContentPackage.package_id == Package.id)
            .where(ContentPackage.content_id.in_(content_ids))
        )
    ).all()
    pkg_map: dict[int, list[str]] = {}
    for cid, pname in pkg_rows:
        pkg_map.setdefault(cid, []).append(pname)

    # Provider + License
    lic_rows = (
        await db.execute(
            select(LicenseContent.content_id, License.start_date, License.end_date, Provider.name)
            .join(License, LicenseContent.license_id == License.id)
            .join(Contract, License.contract_id == Contract.id)
            .join(Provider, Contract.provider_id == Provider.id)
            .where(LicenseContent.content_id.in_(content_ids), License.is_deleted.is_(False))
        )
    ).all()
    prov_map: dict[int, list[str]] = {}
    lic_start_map: dict[int, date] = {}
    lic_end_map: dict[int, date] = {}
    for cid, lstart, lend, pname in lic_rows:
        prov_map.setdefault(cid, []).append(pname)
        # 同一内容绑定多个许可证时：开始日期取最小，结束日期取最大
        if lstart:
            current_start = lic_start_map.get(cid)
            if current_start is None or lstart < current_start:
                lic_start_map[cid] = lstart
        if lend:
            current_end = lic_end_map.get(cid)
            if current_end is None or lend > current_end:
                lic_end_map[cid] = lend

    # Publish/Unpublish 日期（来自 PublishTask 表，非 Content 字段）
    publish_date_map: dict[int, str] = {}
    unpublish_date_map: dict[int, str] = {}
    if content_ids:
        pub_rows = (
            await db.execute(
                select(PublishTask.entity_id, PublishTask.publish_time)
                .where(
                    PublishTask.entity_type == "Content",
                    PublishTask.entity_id.in_(content_ids),
                    PublishTask.publish_time.is_not(None),
                )
                .order_by(PublishTask.publish_time.asc())
            )
        ).all()
        for eid, ptime in pub_rows:
            if eid not in publish_date_map and ptime:
                publish_date_map[eid] = ptime.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S")

        unpub_rows = (
            await db.execute(
                select(PublishTask.entity_id, PublishTask.unpublish_time)
                .where(
                    PublishTask.entity_type == "Content",
                    PublishTask.entity_id.in_(content_ids),
                    PublishTask.unpublish_time.is_not(None),
                )
                .order_by(PublishTask.unpublish_time.desc())
            )
        ).all()
        for eid, utime in unpub_rows:
            if eid not in unpublish_date_map and utime:
                unpublish_date_map[eid] = utime.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S")

    # 字典字段映射（Series Type 已从导出移除，不再查询 SeriesType 字典）
    vod_type_dict = await get_dict_children_by_code(db, "VodType")
    vod_type_map: dict[str, str] = {item.code: item.name for item in vod_type_dict}
    language_dict = await get_dict_children_by_code(db, "Language")
    language_map: dict[str, str] = {item.code: item.name for item in language_dict}
    rating_level_dict = await get_dict_children_by_code(db, "RatingLevel")
    rating_level_map: dict[str, str] = {item.code: item.name for item in rating_level_dict}
    advice_dict = await get_dict_children_by_code(db, "Advice")
    advice_map: dict[str, str] = {item.code: item.name for item in advice_dict}
    metalayout_dict = await get_dict_children_by_code(db, "Metalayout")
    metalayout_map: dict[str, str] = {item.code: item.name for item in metalayout_dict}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Archives"

    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    # 富文本字体：字段名白色 + (*) 红色（与 VOD 导出一致）
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont
    base_inline = InlineFont(rFont="Calibri", b=True, color="FFFFFF")
    mark_inline = InlineFont(rFont="Calibri", b=True, color="FF0000")

    for col, header in enumerate(ARCHIVE_EXPORT_HEADERS, 1):
        cell = ws.cell(row=1, column=col)
        if header.endswith("(*)"):
            cell.value = CellRichText(
                TextBlock(base_inline, header[:-3]),
                TextBlock(mark_inline, "(*)"),
            )
        else:
            cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 列宽（48 列）
    col_widths = [
        12, 14, 30, 24, 14, 12,                   # Content 主表（6）
        14, 12,                                    # Ingest Status / Is Discarded（2）
        20, 24, 20, 20,                            # 归档特有：Channel/Program/Begin/End（4）
        20, 16, 20, 20, 20, 20,                    # 关联（6）
        20, 20, 20, 20, 20,                        # Provider/License/Publish（5）
        20, 16, 16, 20,                            # 元数据-字典
        20, 20, 16, 16, 12,                        # 元数据-名称
        40, 20,                                    # 元数据-描述
        12, 16, 16,                                # 元数据-评分
        20, 20, 12, 12,                            # 元数据-音视频
        20, 16, 12, 12,                            # 元数据-标识（4）
        40, 20, 20,                                # 章节 + 审计
    ]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    for row_idx, r in enumerate(rows, 2):
        # Content 主表（1-6）；Series Type 已移除（统一以 Content Type 区分）
        ws.cell(row=row_idx, column=1, value=r.id)
        ws.cell(row=row_idx, column=2, value=r.content_type or "")
        ws.cell(row=row_idx, column=3, value=r.title or "")
        ws.cell(row=row_idx, column=4, value=parent_name_map.get(r.parent_id, "") if r.parent_id else "")
        # Series Ordinal：归档产物自身字段为空，需从来源节目单的 ScheduleMetadata 取
        sched_meta_for_archive = (
            schedule_meta_by_schedule_id.get(r.source_schedule_id)
            if r.source_schedule_id else None
        )
        archive_series_ordinal = (
            sched_meta_for_archive.series_ordinal
            if sched_meta_for_archive and sched_meta_for_archive.series_ordinal is not None
            else None
        )
        ws.cell(row=row_idx, column=5, value=archive_series_ordinal if archive_series_ordinal is not None else "")
        ws.cell(row=row_idx, column=6, value=r.sequence if r.sequence is not None else "")

        # 主表扩展（7-8）：Ingest Status / Is Discarded
        ws.cell(row=row_idx, column=7, value=r.status or "")
        ws.cell(row=row_idx, column=8, value="YES" if r.is_discarded else "NO")

        # 归档特有（9-12）：来源节目单信息（Channel Name / Program Name / Begin Time / End Time）
        schedule_src = schedule_map.get(r.source_schedule_id) if r.source_schedule_id else None
        if schedule_src:
            ws.cell(row=row_idx, column=9, value=channel_name_map.get(schedule_src.parent_id, "") if schedule_src.parent_id else "")
            ws.cell(row=row_idx, column=10, value=schedule_src.title or "")
            ws.cell(row=row_idx, column=11, value=schedule_src.begin_time.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if schedule_src.begin_time else "")
            ws.cell(row=row_idx, column=12, value=schedule_src.end_time.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if schedule_src.end_time else "")
        else:
            ws.cell(row=row_idx, column=9, value="")
            ws.cell(row=row_idx, column=10, value="")
            ws.cell(row=row_idx, column=11, value="")
            ws.cell(row=row_idx, column=12, value="")

        # 关联（13-18）：Genre/Type/Tags/Custom Tags/Category/Package
        ws.cell(row=row_idx, column=13, value=", ".join(genre_map.get(r.id, [])))
        meta = program_meta_map.get(r.id) if r.content_type in movie_type_ids else series_meta_map.get(r.id)
        type_name = type_name_map.get(meta.type_id, "") if meta and meta.type_id else ""
        ws.cell(row=row_idx, column=14, value=type_name)
        ws.cell(row=row_idx, column=15, value=", ".join(tags_map.get(r.id, [])))  # Tags（元数据中的 tag_ids）
        ws.cell(row=row_idx, column=16, value=", ".join(custom_tag_map.get(r.id, [])))  # Custom Tags（中间表）
        ws.cell(row=row_idx, column=17, value=", ".join(cat_map.get(r.id, [])))
        ws.cell(row=row_idx, column=18, value=", ".join(pkg_map.get(r.id, [])))

        # 版权/发布（19-23）：Provider / License / Publish
        ws.cell(row=row_idx, column=19, value=", ".join(prov_map.get(r.id, [])))
        ws.cell(row=row_idx, column=20, value=lic_start_map.get(r.id, ""))
        ws.cell(row=row_idx, column=21, value=lic_end_map.get(r.id, ""))
        ws.cell(row=row_idx, column=22, value=unpublish_date_map.get(r.id, ""))
        ws.cell(row=row_idx, column=23, value=publish_date_map.get(r.id, ""))

        # 元数据-字典（24-27）
        vod_type_codes = getattr(meta, "vod_type", None) if meta else None
        language_code = getattr(meta, "language", None) if meta else None
        rating_level_code = getattr(meta, "rating_level", None) if meta else None
        advice_codes = getattr(meta, "advice", None) if meta else None
        audio_lang_codes = getattr(meta, "audio_lang", None) if meta else None
        subtitle_lang_codes = getattr(meta, "subtitle_lang", None) if meta else None
        keywords_arr = getattr(meta, "keywords", None) if meta else None
        metalayout_code = getattr(meta, "metalayout", None) if meta else None

        ws.cell(row=row_idx, column=24, value=", ".join(vod_type_map.get(c, c) for c in vod_type_codes) if vod_type_codes else "")
        ws.cell(row=row_idx, column=25, value=language_map.get(language_code, language_code) if language_code else "")
        ws.cell(row=row_idx, column=26, value=rating_level_map.get(rating_level_code, rating_level_code) if rating_level_code else "")
        ws.cell(row=row_idx, column=27, value=", ".join(advice_map.get(c, c) for c in advice_codes) if advice_codes else "")

        # 元数据-名称（28-32）
        ws.cell(row=row_idx, column=28, value=str(getattr(meta, "sort_name", None) or ""))
        ws.cell(row=row_idx, column=29, value=str(getattr(meta, "original_name", None) or ""))
        ws.cell(row=row_idx, column=30, value=str(getattr(meta, "original_country", None) or ""))
        ws.cell(row=row_idx, column=31, value=str(getattr(meta, "short_title", None) or ""))
        ws.cell(row=row_idx, column=32, value=getattr(meta, "release_year", None) if meta and meta.release_year is not None else "")

        # 元数据-描述（33-34）
        ws.cell(row=row_idx, column=33, value=str(getattr(meta, "description", None) or ""))
        ws.cell(row=row_idx, column=34, value=str(getattr(meta, "studio", None) or ""))

        # 元数据-评分（35）：RatingType/RatingId 已移除
        ws.cell(row=row_idx, column=35, value=str(getattr(meta, "rating", None) or ""))

        # 元数据-音视频（36-39）
        ws.cell(row=row_idx, column=36, value=", ".join(language_map.get(c, c) for c in audio_lang_codes) if audio_lang_codes else "")
        ws.cell(row=row_idx, column=37, value=", ".join(language_map.get(c, c) for c in subtitle_lang_codes) if subtitle_lang_codes else "")
        ws.cell(row=row_idx, column=38, value=getattr(meta, "begin_duration", None) if meta and meta.begin_duration is not None else "")
        ws.cell(row=row_idx, column=39, value=getattr(meta, "end_duration", None) if meta and meta.end_duration is not None else "")

        # 元数据-标识（40-43）
        ws.cell(row=row_idx, column=40, value=_format_keywords_export(keywords_arr))
        ws.cell(row=row_idx, column=41, value=metalayout_map.get(metalayout_code, metalayout_code) if metalayout_code else "")
        # StatusFlag：meta 不存在时留空，存在时输出 YES/NO
        if meta:
            ws.cell(row=row_idx, column=42, value="YES" if getattr(meta, "status_flag", False) else "NO")
            # SeriesFlag：连续剧标识（0=VOD/MOVIE, 1=Series/EPISODE）
            ws.cell(row=row_idx, column=43, value=getattr(meta, "series_flag", None))

        # 元数据-章节（44）
        sections_info = getattr(meta, "sections_info", None) if meta else None
        ws.cell(row=row_idx, column=44, value=json.dumps(sections_info, ensure_ascii=False) if sections_info else "")

        # 审计（45-46）
        ws.cell(row=row_idx, column=45, value=r.created_at.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if r.created_at else "")
        ws.cell(row=row_idx, column=46, value=r.updated_at.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if r.updated_at else "")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


# ─── 归档内容 Excel 批量导入（节目单归档） ─────────────────────────────

# 归档导入模板表头（33 列）—— 独立定义，不再复用 VOD 导入模板：
# - Content Type 列已移除：归档产物类型由 Series Type 决定（0→MOVIE/1→SERIES+EPISODE/
#   2→SEASON+SEASON_SERIES+EPISODE），Content Type 为归档派生结果，无需导入；
# - Content ID = 归档内容 ID（有值→定位已有归档内容仅更新元数据；留空→按 Schedule ID 归档创建）；
# - Series Type 必填（归档创建分支）；Parent Name 必须匹配已存在父类（不自动新建，
#   type=1 → 顶层 SERIES、type=2 → 总季下单季 SERIES/SEASON_SERIES），
#   Series Ordinal/Sequence 复用为归档弹窗字段；
# - RatingType/RatingId 已移除（评分来源信息不通过导入维护）。
# 追加 1 列：Schedule ID（待归档节目单 ID，归档创建时必填）。
ARCHIVE_IMPORT_HEADERS = [
    # Content 主表（6）：Content Type 已移除（归档形态由 Series Type 决定）
    "Content ID", "Content Name(*)", "Parent Name",
    "Series Type(*)", "Series Ordinal", "Sequence",
    # 关联（6）：Genre/Type/Tags/Custom Tags/Category/Package
    "Genre(*)", "Type(*)", "Tags", "Custom Tags", "Category", "Package",
    # 元数据-字典（4）
    "VodType(*)", "Language", "RatingLevel(*)", "Advice",
    # 元数据-名称（5）
    "SortName", "OriginalName", "OriginalCountry", "ShortTitle", "ReleaseYear",
    # 元数据-描述（2）
    "Description", "Studio",
    # 元数据-评分（1）：RatingType/RatingId 已移除
    "Rating",
    # 元数据-音视频（4）
    "AudioLang", "SubtitleLang", "BeginDuration", "EndDuration",
    # 元数据-标识（3）
    "Keywords", "Metalayout(*)", "StatusFlag",
    # 元数据-章节（1）
    "SectionsInfo",
    # 归档特有（1）
    "Schedule ID(*)",
]


class ArchiveImportError(BaseModel):
    """归档导入行级校验错误。"""
    row: int
    errors: list[str] = []


class ArchiveImportResult(BaseModel):
    """归档批量导入结果。

    字段：
        total    总行数（不含表头）
        created  归档创建数（Schedule ID 定位节目单并执行归档）
        updated  元数据更新数（Content ID 定位已有归档内容）
        skipped  跳过行数（定位失败/校验失败/归档冲突）
        errors   行级错误明细
    """
    total: int
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[ArchiveImportError] = []


class _ArchiveRowSkipError(Exception):
    """归档导入行跳过异常：触发 savepoint 回滚，仅回滚当前行。"""

    def __init__(self, errors: list[str]):
        self.errors = errors


async def _process_archive_row(
    db: AsyncSession,
    row: tuple,
    header_indices: dict[str, int],
    dict_caches: dict[str, dict[str, str]],
    genre_cache: dict[str, int],
    type_cache: dict[str, int],
    tag_cache: dict[str, int],
    sys_tag_cache: dict[str, int],
    cat_cache: dict[str, int],
    pkg_cache: dict[str, int],
    series_type_map: dict[str, str],
    processed_by: str,
) -> bool:
    """处理单行归档导入：定位归档内容/节目单 → 归档创建（可选）→ VOD 元数据覆盖。

    - Content ID（归档内容 ID）有值 → 仅更新该归档内容的元数据（返回 False）；
    - 否则 Schedule ID（节目单 ID）定位待归档节目单 → 执行归档 → 元数据覆盖（返回 True）。
    失败抛出 _ArchiveRowSkipError（由调用方回滚当前行 savepoint）。
    """
    errors: list[str] = []

    def cell(key: str):
        idx = header_indices.get(key)
        return row[idx] if idx is not None and idx < len(row) else None

    # ── 1. 归档弹窗字段解析（格式校验，不依赖数据库）──
    # Series Type：按字典名称匹配（0=No/1=Series/2=Season Series），兼容直接填数字
    series_type: Optional[int] = None
    st_raw = _cell_str(cell("Series Type"))
    if st_raw:
        code = series_type_map.get(st_raw)
        if code is None and st_raw in ("0", "1", "2"):
            code = st_raw
        if code is None:
            errors.append(f"Series Type not found: {st_raw}")
        else:
            series_type = int(code)

    def _int_cell(key: str, label: str) -> Optional[int]:
        raw = cell(key)
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            errors.append(f"Invalid {label}: {raw}")
            return None

    sequence = _int_cell("Sequence", "Sequence")
    series_ordinal = _int_cell("Series Ordinal", "Series Ordinal")

    # ── 2. 定位：Content ID（归档内容 ID）有值→更新分支；否则 Schedule ID（节目单 ID）→归档分支 ──
    archive_content_id: Optional[int] = None
    schedule: Optional[Content] = None
    content_id_raw = cell("Content ID")
    schedule_id_raw = cell("Schedule ID")

    schedule_id: Optional[int] = None
    if schedule_id_raw is not None and str(schedule_id_raw).strip() != "":
        try:
            schedule_id = int(str(schedule_id_raw).strip())
        except (ValueError, TypeError):
            errors.append(f"Invalid Schedule ID: {schedule_id_raw}")

    if content_id_raw is not None and str(content_id_raw).strip() != "":
        # 更新分支：Content ID = 归档内容 ID，定位已有归档产物仅更新元数据
        try:
            cid = int(str(content_id_raw).strip())
        except (ValueError, TypeError):
            cid = None
            errors.append(f"Invalid Content ID: {content_id_raw}")
        if cid is not None:
            existing = (
                await db.execute(
                    select(Content).where(
                        Content.id == cid,
                        Content.content_type != ContentType.SCHEDULE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                errors.append(f"Archive content not found: {cid}")
            else:
                archive_content_id = existing.id
                # Schedule ID 填写时校验与归档产物的源节目单一致
                if schedule_id is not None:
                    if existing.source_schedule_id is None:
                        errors.append(
                            f"Content {cid} is not an archive content (no source schedule)"
                        )
                    elif existing.source_schedule_id != schedule_id:
                        errors.append(
                            f"Schedule ID mismatch: content {cid} is archived from "
                            f"schedule {existing.source_schedule_id}"
                        )
    else:
        # 归档分支：Schedule ID（节目单 ID）必填，定位待归档节目单
        if schedule_id is None:
            errors.append("Schedule ID is required")
        else:
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
            if schedule is None:
                errors.append(f"Schedule not found: {schedule_id}")
            else:
                # 已有有效归档产物（未删除/未废弃）→ 拒绝导入，提示改走更新分支
                existing_archive = (
                    await db.execute(
                        select(Content.id).where(
                            Content.source_schedule_id == schedule_id,
                            Content.is_deleted.is_(False),
                            Content.is_discarded.is_(False),
                        )
                    )
                ).scalar_one_or_none()
                if existing_archive is not None:
                    errors.append(
                        f"Schedule already archived: content {existing_archive} "
                        f"(use Content ID to update it)"
                    )

    # ── Series Type 必填校验（归档创建分支，与 VOD 导入口径一致）──
    # Series Type 必填：决定归档产物形态（0=MOVIE/1=SERIES+EPISODE/2=SEASON+SEASON_SERIES+EPISODE）。
    # 0=独立节目（MOVIE），其余归档字段不需要；1=连续剧单集，Parent Name（连续剧名称）+
    # Sequence（第几集）必填；2=季播单集，Parent Name+Series Ordinal（第几季）必填。
    # 父类必须已存在：按 Parent Name（type=2 另加 Series Ordinal）查找已有父类，
    # 找不到直接报错跳行，不自动新建（新建父类仅限手动归档入口）。
    # 更新分支（Content ID 定位已有产物）不校验。
    parent_content_id: Optional[int] = None
    if archive_content_id is None:
        if not st_raw:
            errors.append("Series Type is required")
        _parent_name = _cell_str(cell("Parent Name"))
        if series_type in (1, 2) and not _parent_name:
            errors.append("Parent Name is required when Series Type is 1 or 2")
        if series_type in (1, 2) and sequence is None:
            errors.append("Sequence is required when Series Type is 1 or 2")
        if series_type == 2 and series_ordinal is None:
            errors.append("Series Ordinal is required when Series Type is 2")
        # 父类存在性校验（仅在必填校验通过后执行，避免误导性错误）
        if not errors and series_type == 1:
            _parent = (
                await db.execute(
                    select(Content.id).where(
                        Content.title == _parent_name,
                        Content.content_type == ContentType.SERIES.value,
                        Content.parent_id.is_(None),
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    ).limit(1)
                )
            ).scalar_one_or_none()
            if _parent is None:
                errors.append(f"Parent not found: {_parent_name}")
            else:
                parent_content_id = _parent
        elif not errors and series_type == 2:
            # 仅按单季名称匹配（Series Ordinal 不参与查找，避免与存储值不一致时误报）
            _parent = await _find_series_by_name_under_season(db, _parent_name)
            if _parent is None:
                errors.append(f"Parent not found: {_parent_name}")
            else:
                parent_content_id = _parent.id

    if errors:
        raise _ArchiveRowSkipError(errors)

    is_created = False
    if archive_content_id is None:
        # 归档创建：复用单条归档全部校验与产物创建逻辑
        # cutv_enable=True：与手动归档弹窗行为对齐（归档成功后回写节目单 cutv_enable=True，
        # 节目单详情的 Archived tab 依赖该字段才显示）
        req = ArchiveRequest(
            schedule_id=schedule.id,
            mode="now",
            series_type=series_type,
            series_name=_cell_str(cell("Parent Name")) or None,
            # 父类已在上文校验存在，直接以 Content.id 传入确保精确挂载到已有父类
            # （type=1 → SERIES id；type=2 → SEASON_SERIES id）
            series_id=str(parent_content_id) if parent_content_id else None,
            sequence=sequence,
            series_ordinal=series_ordinal,
            cutv_enable=True,
        )
        try:
            archive_resp = await archive_schedule(db, req, processed_by=processed_by)
        except BusinessException as e:
            raise _ArchiveRowSkipError([e.message])
        archive_content_id = archive_resp.archive_content_id
        if archive_content_id is None:
            raise _ArchiveRowSkipError(["Archive failed: no archive content created"])
        is_created = True

    # ── 4. VOD 元数据列覆盖归档产物 ──
    # Content Name 填写时覆盖归档产物标题（默认为节目单标题）
    content_name = _cell_str(cell("Content Name"))
    archive_content = (
        await db.execute(select(Content).where(Content.id == archive_content_id))
    ).scalar_one()
    if content_name:
        archive_content.title = content_name
    # 归档内容 CUTV Enable 固定为开
    archive_content.cutv_enable = True

    # Genre（严格匹配）→ content_genre 中间表
    genre_ids = _resolve_basic_data_strict(cell("Genre"), genre_cache, "Genre", errors)
    # Type（严格匹配）→ ContentMetadata.type_id
    type_name = _cell_str(cell("Type"))
    type_id: Optional[int] = None
    if type_name:
        type_id = type_cache.get(type_name)
        if type_id is None:
            errors.append(f"Type not found: {type_name}")
    # Custom Tags（严格匹配）→ content_custom_tag 中间表
    custom_tag_ids = _resolve_basic_data_strict(cell("Custom Tags"), tag_cache, "Custom Tags", errors)
    # Tags（元数据 tag_ids，严格匹配）
    tags_str = _cell_str(cell("Tags"))
    resolved_tag_ids: list[int] = []
    if tags_str:
        for tn in [t.strip() for t in tags_str.split(",") if t.strip()]:
            tid = sys_tag_cache.get(tn)
            if tid:
                resolved_tag_ids.append(tid)
            else:
                errors.append(f"Tag not found: {tn}")
    # Category / Package（仅匹配）
    cat_ids = _resolve_relation_ids(cell("Category"), cat_cache, "Category", errors)
    pkg_ids = _resolve_relation_ids(cell("Package"), pkg_cache, "Package", errors)

    # 字典字段（严格匹配，不创建）
    vod_type_codes = await resolve_dict_codes_strict(db, "VodType", cell("VodType"), dict_caches, errors, "VodType")
    language_code = await resolve_single_dict_code_strict(db, "Language", cell("Language"), dict_caches, errors, "Language")
    rating_level_code = await resolve_single_dict_code_strict(db, "RatingLevel", cell("RatingLevel"), dict_caches, errors, "RatingLevel")
    advice_codes = await resolve_dict_codes_strict(db, "Advice", cell("Advice"), dict_caches, errors, "Advice")
    audio_lang_codes = await resolve_dict_codes_strict(db, "Language", cell("AudioLang"), dict_caches, errors, "AudioLang")
    subtitle_lang_codes = await resolve_dict_codes_strict(db, "Language", cell("SubtitleLang"), dict_caches, errors, "SubtitleLang")
    metalayout_code = await resolve_single_dict_code_strict(db, "Metalayout", cell("Metalayout"), dict_caches, errors, "Metalayout")

    if errors:
        raise _ArchiveRowSkipError(errors)

    # 文本/数值字段（空值不覆盖）
    sort_name = _cell_str(cell("SortName"))
    original_name = _cell_str(cell("OriginalName"))
    original_country = _cell_str(cell("OriginalCountry"))
    short_title = _cell_str(cell("ShortTitle"))
    release_year_raw = cell("ReleaseYear")
    release_year: Optional[int] = None
    if release_year_raw not in (None, ""):
        try:
            release_year = int(release_year_raw)
        except (ValueError, TypeError):
            errors.append(f"Invalid ReleaseYear: {release_year_raw}")
    description = _cell_str(cell("Description"))
    studio = _cell_str(cell("Studio"))
    rating = _cell_str(cell("Rating"))
    begin_duration_raw = cell("BeginDuration")
    begin_duration: Optional[int] = None
    if begin_duration_raw not in (None, ""):
        try:
            begin_duration = int(begin_duration_raw)
        except (ValueError, TypeError):
            errors.append(f"Invalid BeginDuration: {begin_duration_raw}")
    end_duration_raw = cell("EndDuration")
    end_duration: Optional[int] = None
    if end_duration_raw not in (None, ""):
        try:
            end_duration = int(end_duration_raw)
        except (ValueError, TypeError):
            errors.append(f"Invalid EndDuration: {end_duration_raw}")
    keywords_raw = _cell_str(cell("Keywords"))
    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()] if keywords_raw else None
    status_flag_raw = _cell_str(cell("StatusFlag"))
    status_flag: Optional[bool] = None
    if status_flag_raw:
        status_flag = status_flag_raw.upper() != "NO"
    sections_info_raw = _cell_str(cell("SectionsInfo"))
    sections_info = None
    if sections_info_raw:
        try:
            sections_info = json.loads(sections_info_raw)
        except (json.JSONDecodeError, ValueError):
            errors.append(f"Invalid SectionsInfo JSON: {sections_info_raw}")
        else:
            sections_info, section_errors = normalize_sections_info(sections_info)
            errors.extend(section_errors)

    if errors:
        raise _ArchiveRowSkipError(errors)

    # 归档产物元数据（archive_schedule 已从节目单复制创建 ContentMetadata）
    meta = (
        await db.execute(
            select(ContentMetadata).where(ContentMetadata.content_id == archive_content_id)
        )
    ).scalar_one_or_none()
    if meta is None:
        meta = ContentMetadata(
            content_id=archive_content_id,
            name=content_name or archive_content.title,
            cdr_id=f"Program_{archive_content_id}",
        )
        db.add(meta)
    if type_id is not None:
        meta.type_id = type_id
    if resolved_tag_ids:
        meta.tag_ids = resolved_tag_ids
    # cdr_id 传 None：保留从节目单复制来的值，不覆盖
    _apply_meta_fields(
        meta, vod_type_codes, language_code, rating_level_code,
        advice_codes, audio_lang_codes, subtitle_lang_codes, metalayout_code,
        sort_name, original_name, original_country, short_title, release_year,
        description, studio, rating,
        begin_duration, end_duration, keywords, None, status_flag,
        sections_info,
    )

    # 关联中间表（填了才覆盖）
    if genre_ids:
        await db.execute(delete(ContentGenre).where(ContentGenre.content_id == archive_content_id))
        for gid in genre_ids:
            db.add(ContentGenre(content_id=archive_content_id, genre_id=gid))
    if custom_tag_ids:
        await db.execute(delete(ContentCustomTag).where(ContentCustomTag.content_id == archive_content_id))
        for tid in custom_tag_ids:
            db.add(ContentCustomTag(content_id=archive_content_id, custom_tag_id=tid))
    if cat_ids:
        await db.execute(delete(ContentCategory).where(ContentCategory.content_id == archive_content_id))
        for cid in cat_ids:
            db.add(ContentCategory(content_id=archive_content_id, category_id=cid))
    if pkg_ids:
        await db.execute(delete(ContentPackage).where(ContentPackage.content_id == archive_content_id))
        for pid in pkg_ids:
            db.add(ContentPackage(content_id=archive_content_id, package_id=pid))

    # ── 5. 状态流转（与 VOD 导入口径对齐）──
    # 元数据完整性实时校验 → 写 Metadata 流程节点记录（Passed/Pending）
    await db.flush()  # 确保元数据覆盖已落 session，校验读取到最新数据
    from app.internal.cms_biz_orchestration.services.metadata_validation_service import check_metadata_complete
    meta_complete, _missing = await check_metadata_complete(
        db, archive_content_id, archive_content.content_type,
    )
    await complete_process_and_update_status(
        db,
        content_id=archive_content_id,
        content_type=archive_content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed" if meta_complete else "Pending",
        info="归档导入",
    )
    # 更新分支（Content ID 定位已有产物）：已发布/准备发布等内容编辑后回退 InProgress 重走审核
    if not is_created:
        await rollback_after_published_edit(
            db,
            content_id=archive_content_id,
            content_type=archive_content.content_type,
            edited_by=processed_by,
            edit_info="归档导入更新元数据",
        )

    return is_created


async def import_archives_excel(
    db: AsyncSession,
    file: UploadFile,
    processed_by: str = "import",
) -> ArchiveImportResult:
    """从 Excel 批量归档节目单（33 列独立模板，不再复用 VOD 模板：Content Type 与
    RatingType/RatingId 已移除，归档产物类型由 Series Type 决定）。

    每行流程：
    1. Content ID（归档内容 ID）有值 → 定位已有归档产物，仅更新元数据；
       否则 Schedule ID（节目单 ID）定位待归档节目单；
    2. 归档创建时以 Series Type/Parent Name=连续剧名称/Sequence/Series Ordinal
       构造归档请求，复用 archive_schedule 的全部校验（已发布、未归档、
       集序号冲突等）与归档产物创建逻辑；
    3. 归档/定位成功后，VOD 元数据列（Genre/Type/字典/文本字段）覆盖写入归档产物。
    - 行级 savepoint：某行失败仅回滚该行，不影响其他行；
    - 表头严格校验：缺失或多余列 → 整个文件拒绝导入。
    """
    import openpyxl

    contents = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(contents))
    ws = wb.active

    result = ArchiveImportResult(total=max(0, ws.max_row - 1))

    # ── 表头严格校验（35 列）──
    actual_headers = [cell.value for cell in ws[1]]
    normalized_headers = [_normalize_vod_header(h) for h in actual_headers]
    expected_headers = [_normalize_vod_header(h) for h in ARCHIVE_IMPORT_HEADERS]
    missing = [h for h in expected_headers if h not in normalized_headers]
    if missing:
        raise BusinessException(
            ErrorCode.INVALID_FILE_FORMAT,
            get_msg("INVALID_FILE_FORMAT") + f" (missing: {', '.join(missing)})",
        )
    extra = [h for h in normalized_headers if h and h not in expected_headers]
    if extra:
        raise BusinessException(
            ErrorCode.INVALID_FILE_FORMAT,
            get_msg("INVALID_FILE_FORMAT") + f" (unexpected columns: {', '.join(extra)})",
        )
    header_indices: dict[str, int] = {h: i for i, h in enumerate(normalized_headers) if h}

    # ── 预加载缓存（全部严格匹配，不自动创建）──
    dict_caches: dict[str, dict[str, str]] = {}
    genre_cache: dict[str, int] = {
        g.name: g.id for g in
        (await db.execute(select(Genre).where(Genre.is_deleted.is_(False)))).scalars().all()
    }
    type_cache: dict[str, int] = {
        t.name: t.id for t in
        (await db.execute(select(ContentTypeModel).where(ContentTypeModel.is_deleted.is_(False)))).scalars().all()
    }
    tag_cache: dict[str, int] = {
        t.name: t.id for t in
        (await db.execute(select(CustomTag).where(CustomTag.is_deleted.is_(False)))).scalars().all()
    }
    sys_tag_cache: dict[str, int] = {
        t.name: t.id for t in
        (await db.execute(select(Tag).where(Tag.is_deleted.is_(False)))).scalars().all()
    }
    cat_cache: dict[str, int] = {
        c.name: c.id for c in
        (await db.execute(select(Category).where(Category.is_deleted.is_(False)))).scalars().all()
    }
    pkg_cache: dict[str, int] = {
        p.name: p.id for p in
        (await db.execute(select(Package).where(Package.is_deleted.is_(False)))).scalars().all()
    }
    # SeriesType 字典：name → code（code 为 "0"/"1"/"2"）
    series_type_map: dict[str, str] = {
        item.name: item.code for item in await get_dict_children_by_code(db, "SeriesType")
    }

    # ── 逐行处理（行级 savepoint）──
    for row_idx in range(2, ws.max_row + 1):
        row = tuple(cell.value for cell in ws[row_idx])
        # 空行静默跳过
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        try:
            async with db.begin_nested():
                is_created = await _process_archive_row(
                    db, row, header_indices,
                    dict_caches, genre_cache, type_cache, tag_cache, sys_tag_cache,
                    cat_cache, pkg_cache, series_type_map,
                    processed_by,
                )
        except _ArchiveRowSkipError as e:
            result.errors.append(ArchiveImportError(row=row_idx, errors=e.errors))
            result.skipped += 1
            continue
        except Exception as e:
            logger.exception(f"归档导入第 {row_idx} 行异常: {e}")
            result.errors.append(ArchiveImportError(row=row_idx, errors=[f"处理异常: {e}"]))
            result.skipped += 1
            continue
        if is_created:
            result.created += 1
        else:
            result.updated += 1

    await db.commit()
    return result


def generate_archive_import_template() -> bytes:
    """生成归档导入模板 Excel 文件（33 列，独立定义，不再复用 VOD 模板）。

    - Content Type 列已移除：归档产物类型由 Series Type 决定，Content Type 为派生结果
    - RatingType/RatingId 列已移除（评分来源信息不通过导入维护）
    - 必填字段表头带红色 (*) 标记（导入时按表头名称匹配，(*) 会被忽略）
    - 含两行示例数据（独立节目归档 / 连续剧单集归档）及填写说明（Instructions 工作表）
    """
    import openpyxl
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Archive Import"

    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    base_inline = InlineFont(rFont="Calibri", b=True, color="FFFFFF")
    mark_inline = InlineFont(rFont="Calibri", b=True, color="FF0000")

    for col, header in enumerate(ARCHIVE_IMPORT_HEADERS, 1):
        cell = ws.cell(row=1, column=col)
        if header.endswith("(*)"):
            cell.value = CellRichText(
                TextBlock(base_inline, header[:-3]),
                TextBlock(mark_inline, "(*)"),
            )
        else:
            cell.value = header
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    # 列宽（33 列）
    col_widths = [
        12, 30, 24, 14, 14, 12,                   # Content 主表（6）：Content Type 已移除
        20, 16, 20, 20, 20, 20,                    # 关联（6）
        20, 16, 16, 20,                            # 元数据-字典
        20, 20, 16, 16, 12,                        # 元数据-名称
        40, 20,                                    # 元数据-描述
        12,                                        # 元数据-评分（1）：RatingType/RatingId 已移除
        20, 20, 12, 12,                            # 元数据-音视频
        20, 16, 12,                                # 元数据-标识（3）
        40,                                        # 元数据-章节
        16,                                        # 节目单 ID（1）
    ]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    # 示例数据行（33 列；Content Type 已移除，归档形态由 Series Type 决定）
    example_rows = [
        # 示例 1：独立节目归档（Series Type=0 → 归档为 MOVIE），Schedule ID 定位待归档节目单
        [
            "", "Sample Movie", "",
            "0", "", "",
            "Action,Adventure", "Movie", "", "Tag1,Tag2", "Movies", "Basic Package",
            "Film", "English", "PG", "Violence",
            "Sample Movie", "Sample Movie Original", "US", "Sample", "2025",
            "A sample movie description", "Sample Studio",
            "8.5",
            "English", "English", "0", "7200",
            "sample,movie", "M0", "YES",
            "",
            "2001",
        ],
        # 示例 2：连续剧单集归档（Series Type=1 → 查找/创建 SERIES 后创建 EPISODE），Parent Name=连续剧名称
        [
            "", "Sample Episode 01", "Sample Series",
            "1", "", "1",
            "Drama", "Series", "", "", "", "",
            "Serial", "English", "PG", "",
            "Sample Episode 01", "Sample Series Original", "US", "", "2025",
            "A sample episode description", "",
            "",
            "", "", "", "",
            "", "M0", "YES",
            "",
            "2002",
        ],
    ]
    for row_idx, row_data in enumerate(example_rows, 2):
        for col, val in enumerate(row_data, 1):
            ws.cell(row=row_idx, column=col, value=val)

    # 填写说明页（仅英文）
    notes_ws = wb.create_sheet("Instructions")
    notes = [
        "Instructions",
        "",
        "1. This template is standalone (33 columns): the Content Type column has been removed because "
        "the archive product type is determined by Series Type (Content Type is a derived result); "
        "the last column is Schedule ID.",
        "2. Content ID (archive content ID): when filled, locates an existing archive content and only updates "
        "its metadata (no archiving); when empty, archiving is performed via Schedule ID. "
        "If Schedule ID is also filled, it must match the content's source schedule.",
        "3. Schedule ID (required for archive creation): the ID of the schedule to archive. "
        "Only published and non-archived schedules can be archived; otherwise the row is skipped.",
        "4. Series Type (required for archive creation): determines the archive product type: "
        "0=standalone program (archived as MOVIE), "
        "1=series episode (attach to an existing SERIES, then create EPISODE), "
        "2=season series episode (attach to an existing season SERIES under its SEASON, then create EPISODE).",
        "5. Archive fields (required when creating via Schedule ID): "
        "Series Type is required; "
        "Parent Name=series name (required when Series Type is 1 or 2) and MUST match an existing parent "
        "(type 1: existing top-level SERIES; type 2: existing season SERIES under a SEASON, matched by name); "
        "the parent is NOT created automatically - rows with an unmatched parent are skipped; "
        "Sequence=episode number (required when Series Type is 1 or 2); "
        "Series Ordinal=season number (required when Series Type is 2). "
        "No such requirement for update rows (Content ID filled).",
        "6. Other columns (same as the VOD import template): filled values overwrite the archive content's metadata; "
        "empty values keep the metadata copied from the schedule. "
        "Genre/Type/Tags/Custom Tags/Category/Package/VodType/Language/RatingLevel/Advice/AudioLang/SubtitleLang/Metalayout "
        "must match existing data in the system (separate multiple values with commas); "
        "rows that fail to match will be skipped.",
        "7. Strict mode: missing or unexpected columns (e.g. the Content Type or RatingType/RatingId columns in old files) "
        "will cause the entire file to be rejected. "
        "A failed row is rolled back individually without affecting other rows.",
        "8. SectionsInfo is a JSON array, e.g. [{\"type\": 3, \"action\": 0, \"tag\": \"chapter\", \"start\": 0, \"end\": 1800}]. "
        "type: 1=intro/2=ad/3=chapter; action: 0=no skip/1=skip; tag is the label text; start/end are integer seconds.",
    ]
    for row_idx, note in enumerate(notes, 1):
        notes_ws.cell(row=row_idx, column=1, value=note)
    notes_ws.column_dimensions["A"].width = 120
    notes_ws["A1"].font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
