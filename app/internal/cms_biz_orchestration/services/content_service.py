"""
内容（Content）业务逻辑层 — 交易管理视角。

职责：
- Content 的 CRUD（新增、查询、编辑、软删除）
- SERIES/SEASON 创建时自动批量创建子节点（EPISODE/SERIES）
- 与许可证关联信息的查询（内容已关联的许可证列表）
- 统计无许可证内容数量

业务规则：
1. 同一 content_type 下同名内容在同一父节点下不能重名
2. 新建 SERIES 时，按 volumn_count 自动创建 EPISODE 子节点（序号 1..N）
3. 新建 SEASON 时，按 season_details 自动创建 SERIES 子节点，每个 SERIES 再创建对应集数的 EPISODE
4. 软删除：is_deleted=True
"""

import io
import json
import uuid
from datetime import date, datetime
from typing import Optional

from loguru import logger
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import UploadFile
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel

from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException
from app.config import app_tz

from app.internal.cms_biz_metada.models.basic import Genre, CustomTag, Category, Picture, ContentType as ContentTypeModel, Tag
from app.internal.cms_biz_package.models import ContentType, ContentStatus
from app.internal.cms_biz_package.models.package import Content, ContentGenre, ContentPackage, Package, ContentCustomTag, ContentCategory
from app.internal.cms_biz_package.models.task import Task
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_scp.models.trade import Contract, License, LicenseContent, Provider
from app.internal.cms_biz_orchestration.models.content_metadata import ContentMetadata, SeriesMetadata, ChannelMetadata, ScheduleMetadata
from app.internal.cms_biz_orchestration.schemas.content_metadata import normalize_sections_info
from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.internal.cms_biz_orchestration.services.dict_utils import resolve_dict_codes_strict, resolve_single_dict_code_strict
from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
from app.internal.cms_biz_orchestration.schemas.content import (
    AdjacentContentResponse,
    ContentCreate,
    ContentListItem,
    ContentSimpleItem,
    VodContentListItem,
    ContentUpdate,
    ContentLicenseRef,
    ContentDetailResponse,
    ContentTaskAssignees,
)
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.data_auth_filter import apply_content_data_auth
from app.internal.cms_biz_system.services.dict_service import get_dict_children_by_code
from app.internal.cms_biz_system.models.dict import DictNode


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


async def _get_content_or_404(db: AsyncSession, content_id: int) -> Content:
    """查询内容，不存在则抛 404。"""
    c = (
        await db.execute(
            select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if not c:
        raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))
    return c


async def _build_license_info(db: AsyncSession, content_id: int) -> tuple[int, Optional[str], Optional[str]]:
    """
    返回 (license_count, license_start, license_end)。
    license_start = 最早 start_date；license_end = 最晚 end_date。
    """
    rows = (
        await db.execute(
            select(License.start_date, License.end_date)
            .join(LicenseContent, LicenseContent.license_id == License.id)
            .where(
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
                License.is_deleted.is_(False),
            )
        )
    ).all()

    valid = [(r.start_date, r.end_date) for r in rows if not (r.start_date is None and r.end_date is None)]
    count = len(rows)

    starts = [r[0] for r in valid if r[0] is not None]
    ends = [r[1] for r in valid if r[1] is not None]

    license_start = str(min(starts)) if starts else None
    license_end = str(max(ends)) if ends else None

    return count, license_start, license_end


async def _get_custom_tags(db: AsyncSession, content_id: int) -> tuple[list[int], list[str]]:
    """查询内容关联的自定义标签 ID 列表和名称列表。"""
    from loguru import logger
    
    logger.info(f"[_get_custom_tags] 查询 content_id={content_id} 的自定义标签")
    
    rows = (
        await db.execute(
            select(ContentCustomTag.custom_tag_id, CustomTag.name)
            .join(CustomTag, ContentCustomTag.custom_tag_id == CustomTag.id)
            .where(ContentCustomTag.content_id == content_id, CustomTag.is_deleted.is_(False))
        )
    ).all()
    
    logger.info(f"[_get_custom_tags] content_id={content_id} 查询结果: {len(rows)} 条记录")
    for row in rows:
        logger.info(f"[_get_custom_tags]   - custom_tag_id={row.custom_tag_id}, name={row.name}")
    
    ids = [r.custom_tag_id for r in rows]
    names = [r.name for r in rows]
    return ids, names


async def _build_item(db: AsyncSession, c: Content) -> ContentListItem:
    """将 ORM Content 转换为响应 schema。"""
    genre_ids, genre_name = await _get_genre_names(db, c.id)

    # 父节点标题（EPISODE 显示所属 SERIES 名称等）
    parent_title: Optional[str] = None
    if c.parent_id:
        parent = (
            await db.execute(select(Content.title).where(Content.id == c.parent_id))
        ).scalar_one_or_none()
        parent_title = parent

    license_count, license_start, license_end = await _build_license_info(db, c.id)

    # 获取自定义标签
    custom_tag_ids, custom_tag_names = await _get_custom_tags(db, c.id)

    # 计算 volumn_count：SERIES 统计 EPISODE 子节点数，SEASON 统计 SERIES 子节点数
    volumn_count: Optional[int] = None
    if c.content_type in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.SEASON.value):
        if c.content_type in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
            child_type = ContentType.EPISODE.value
        else:
            child_type = ContentType.SEASON_SERIES.value
        count = (
            await db.execute(
                select(func.count()).select_from(Content).where(
                    Content.parent_id == c.id,
                    Content.content_type == child_type,
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalar_one()
        volumn_count = count

    # 查询元数据名称（channel_metadata / schedule_metadata / program_metadata / series_metadata）
    meta_name: Optional[str] = None
    if c.content_type == ContentType.CHANNEL.value:
        meta_name = (
            await db.execute(
                select(ChannelMetadata.name).where(
                    ChannelMetadata.content_id == c.id,
                    ChannelMetadata.is_deleted.is_(False),
                    ChannelMetadata.is_discarded.is_(False),
                )
            )
        ).scalar_one_or_none()
    elif c.content_type == ContentType.SCHEDULE.value:
        meta_name = (
            await db.execute(
                select(ScheduleMetadata.name).where(
                    ScheduleMetadata.content_id == c.id,
                    ScheduleMetadata.is_deleted.is_(False),
                    ScheduleMetadata.is_discarded.is_(False),
                )
            )
        ).scalar_one_or_none()
    elif c.content_type in (ContentType.MOVIE.value, ContentType.EPISODE.value):
        meta_name = (
            await db.execute(
                select(ContentMetadata.name).where(ContentMetadata.content_id == c.id)
            )
        ).scalar_one_or_none()
    elif c.content_type in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.SEASON.value):
        meta_name = (
            await db.execute(
                select(SeriesMetadata.name).where(SeriesMetadata.content_id == c.id)
            )
        ).scalar_one_or_none()

    # 查询 arrangement 任务的负责人名称与任务起止时间
    # （需求 3.6.6/3.6.7：子内容列表展示"对应的内容编排任务"的进展情况）
    assignee_name: Optional[str] = None
    task_start_time: Optional[datetime] = None
    task_end_time: Optional[datetime] = None
    arr_task = (
        await db.execute(
            select(Task).where(
                Task.content_id == c.id,
                Task.task_type == "arrangement",
                Task.is_deleted.is_(False),
            ).order_by(Task.id.desc()).limit(1)
        )
    ).scalars().first()
    if arr_task:
        task_start_time = arr_task.start_time
        task_end_time = arr_task.end_time
        if arr_task.assignee_id:
            user = (
                await db.execute(
                    select(User).where(User.id == arr_task.assignee_id, User.is_deleted.is_(False))
                )
            ).scalar_one_or_none()
            if user:
                assignee_name = user.display_name or user.username

    return ContentListItem(
        id=c.id,
        content_type=c.content_type,
        title=c.title,
        status=c.status,
        external_id=c.external_id,
        parent_id=c.parent_id,
        parent_title=parent_title,
        genre_ids=genre_ids,
        genre_name=genre_name,
        custom_tag_ids=custom_tag_ids if custom_tag_ids else None,
        custom_tag_names=custom_tag_names if custom_tag_names else None,
        sequence=c.sequence,
        series_ordinal=c.series_ordinal,
        volumn_count=volumn_count,
        begin_time=c.begin_time,
        end_time=c.end_time,
        license_count=license_count,
        license_start=license_start,
        license_end=license_end,
        created_at=c.created_at,
        is_archived=c.is_archived,
        source_schedule_id=c.source_schedule_id,
        is_discarded=c.is_discarded,
        cutv_enable=c.cutv_enable,
        assignee_name=assignee_name,
        task_start_time=task_start_time,
        task_end_time=task_end_time,
    )


# ─── Content CRUD ─────────────────────────────────────────────────────

async def list_contents(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    content_id: Optional[int] = None,
    external_id: Optional[str] = None,
    title: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    statuses: Optional[list[str]] = None,
    genre_ids: Optional[list[int]] = None,
    custom_tag_ids: Optional[list[int]] = None,
    parent_id: Optional[int] = None,
    created_from: Optional[str] = None,
    created_to: Optional[str] = None,
    without_license: bool = False,
    license_start_from: Optional[str] = None,
    license_start_to: Optional[str] = None,
    license_end_from: Optional[str] = None,
    license_end_to: Optional[str] = None,
    is_discarded: Optional[bool] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    current_user: Optional[User] = None,
) -> PaginatedResponse[ContentListItem]:
    """
    查询内容列表（交易视角，分页）。

    输入参数：
        page                页码
        page_size           每页条数
        content_id          内容 ID（精确匹配）
        external_id         外部 ID（精确匹配）
        title               内容标题（模糊）
        content_types       内容类型列表（任一匹配）
        statuses            Ingest 状态列表（任一匹配）
        genre_ids           题材 id 列表（任一匹配）
        custom_tag_ids      自定义标签 id 列表（内容关联的标签与列表有交集即匹配）
        parent_id           父级内容 id
        created_from        创建日期范围下限（YYYY-MM-DD）
        created_to          创建日期范围上限
        without_license     仅返回无关联许可证的内容
        license_start_from  关联许可证开始日期范围下限
        license_start_to    关联许可证开始日期范围上限
        license_end_from    关联许可证结束日期范围下限
        license_end_to      关联许可证结束日期范围上限

    输出：
        PaginatedResponse[ContentListItem]
    """
    logger.info(f"list_contents 入参: page={page}, page_size={page_size}, content_id={content_id}, external_id={external_id}, title={title}, content_types={content_types}, statuses={statuses}, genre_ids={genre_ids}, custom_tag_ids={custom_tag_ids}, parent_id={parent_id}, created_from={created_from}, created_to={created_to}, without_license={without_license}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, is_discarded={is_discarded}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Content).where(Content.is_deleted.is_(False))

    if is_discarded is not None:
        query = query.where(Content.is_discarded.is_(is_discarded))
    else:
        query = query.where(Content.is_discarded.is_(False))

    # 注意：Content Management 列表不过滤数据权限，显示所有内容
    # 数据权限校验在点击跳转详情时进行（前端调用 /data-authorization/check-permission/{content_id}）
    # query = await apply_content_data_auth(db, current_user, query)

    if content_id is not None:
        query = query.where(Content.id == content_id)

    if external_id is not None and title:
        query = query.where(or_(Content.external_id == external_id, Content.title.ilike(f"%{title}%")))
    elif external_id is not None:
        query = query.where(Content.external_id == external_id)
    elif title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if statuses:
        query = query.where(Content.status.in_(statuses))
    if genre_ids:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id.in_(genre_ids))
        query = query.where(Content.id.in_(subq))
    if custom_tag_ids:
        sub_ct = select(ContentCustomTag.content_id).where(
            ContentCustomTag.custom_tag_id.in_(custom_tag_ids)
        )
        query = query.where(Content.id.in_(sub_ct))
    if parent_id is not None:
        query = query.where(Content.parent_id == parent_id)
    if created_from:
        query = query.where(Content.created_at >= datetime.fromisoformat(created_from))
    if created_to:
        query = query.where(Content.created_at <= datetime.fromisoformat(created_to + " 23:59:59"))

    # 许可证过滤：先找满足条件的 license_id，再 join content_id
    if without_license:
        # 使用 NOT EXISTS 替代 NOT IN，避免子查询返回 NULL 时的问题
        sub_lc_exists = select(LicenseContent).where(
            LicenseContent.content_id == Content.id,
            LicenseContent.is_deleted.is_(False),
        )
        query = query.where(~sub_lc_exists.exists())

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
    contents = (
        await db.execute(
            query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    items = [await _build_item(db, c) for c in contents]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def get_content(db: AsyncSession, content_id: int) -> ContentDetailResponse:
    """
    查询单个内容详情（含任务指派人信息，用于详情页权限校验）。

    输入：content_id
    输出：ContentDetailResponse（包含 content 详情和 task_assignees）
    """
    logger.info(f"get_content 入参: content_id={content_id}")
    c = await _get_content_or_404(db, content_id)
    content_item = await _build_item(db, c)
    task_assignees = await _get_content_task_assignees(db, content_id)
    return ContentDetailResponse(content=content_item, task_assignees=task_assignees)


async def get_adjacent_content(
    db: AsyncSession,
    content_id: int,
    current_user: Optional[User] = None,
    content_types: Optional[list[str]] = None,
    is_archived: Optional[bool] = None,
) -> AdjacentContentResponse:
    """
    查询当前内容的上一条/下一条内容 ID（按 id 排序）。

    输入：
        content_id   当前内容 id
        current_user 当前用户（用于数据权限过滤）
        content_types 可选，内容类型过滤列表
        is_archived  可选，是否只查询归档内容
    输出：
        AdjacentContentResponse（prev_id / next_id）
    """
    logger.info(f"get_adjacent_content 入参: content_id={content_id}, content_types={content_types}, is_archived={is_archived}")
    await _get_content_or_404(db, content_id)

    query = select(Content.id).where(Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    
    # 添加内容类型过滤
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    
    # 添加归档状态过滤
    if is_archived is not None:
        query = query.where(Content.is_archived == is_archived)
    
    query = await apply_content_data_auth(db, current_user, query)
    query = query.order_by(Content.id)

    result = await db.execute(query)
    ids = result.scalars().all()

    prev_id = None
    next_id = None
    for i, cid in enumerate(ids):
        if cid == content_id:
            if i > 0:
                prev_id = ids[i - 1]
            if i < len(ids) - 1:
                next_id = ids[i + 1]
            break

    return AdjacentContentResponse(prev_id=prev_id, next_id=next_id)


async def _get_content_task_assignees(db: AsyncSession, content_id: int) -> ContentTaskAssignees:
    """
    获取内容关联的任务指派人信息。

    查询 arrangement、review L1/L2/L3 任务的 assignee_id 和 assignee_name。
    """
    # 查询所有任务
    tasks = (
        await db.execute(
            select(Task).where(
                Task.content_id == content_id,
                Task.is_deleted.is_(False),
            ).order_by(Task.id.desc())
        )
    ).scalars().all()

    result = ContentTaskAssignees()

    for task in tasks:
        assignee_name = None
        if task.assignee_id:
            user = (
                await db.execute(
                    select(User).where(User.id == task.assignee_id, User.is_deleted.is_(False))
                )
            ).scalar_one_or_none()
            if user:
                assignee_name = user.display_name or user.username

        if task.task_type == "arrangement":
            result.arrangement_assignee_id = task.assignee_id
            result.arrangement_assignee_name = assignee_name
            result.arrangement_task_status = task.task_status
        elif task.task_type == "review L1":
            result.review_l1_assignee_id = task.assignee_id
            result.review_l1_assignee_name = assignee_name
            result.review_l1_task_status = task.task_status
        elif task.task_type == "review L2":
            result.review_l2_assignee_id = task.assignee_id
            result.review_l2_assignee_name = assignee_name
            result.review_l2_task_status = task.task_status
        elif task.task_type == "review L3":
            result.review_l3_assignee_id = task.assignee_id
            result.review_l3_assignee_name = assignee_name
            result.review_l3_task_status = task.task_status

    return result


async def check_content_title_unique(
    db: AsyncSession,
    title: str,
    content_type: str,
    exclude_id: Optional[int] = None,
) -> None:
    """
    校验内容名称唯一性（同一 content_type 下不能重名，跨频道/父级同样拦截）。

    统一供内容管理新增/编辑、节目单管理新增、节目单 Excel 导入等入口调用，
    命中重名时抛出 CONTENT_NAME_EXISTS 业务异常。

    例外：SCHEDULE（节目单）名称允许重名，不做唯一性校验。
    """
    if content_type == ContentType.SCHEDULE.value:
        return
    q = (
        select(Content.id)
        .where(
            Content.title == title,
            Content.content_type == content_type,
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),
        )
        .limit(1)
    )
    if exclude_id is not None:
        q = q.where(Content.id != exclude_id)
    hit = (await db.execute(q)).scalar_one_or_none()
    if hit is not None:
        raise BusinessException(ErrorCode.CONTENT_NAME_EXISTS, get_msg("CONTENT_NAME_EXISTS"))


async def create_content(db: AsyncSession, data: ContentCreate, processed_by: str | None = None) -> ContentListItem:
    """
    新建内容。

    输入：ContentCreate
    输出：ContentListItem（返回主节点信息）

    业务规则：
    - EPISODE：校验 parent_id 指向有效 SERIES；设置 sequence
    - SERIES  ：自动按 volumn_count 创建 EPISODE 子节点
    - SEASON  ：按 season_details 自动创建 SERIES 子节点，再为每个 SERIES 创建 EPISODE
    - SCHEDULE：校验 parent_id 指向有效 CHANNEL；设置 begin_time/end_time
    - InjectSubContent 流程记录处理人取 processed_by（当前操作人），未传时回退 system
    """
    logger.info(f"create_content 入参: data={data}")
    ctype = data.content_type.upper()

    # ── 校验内容名称唯一性（同一 content_type 下不能重名）─────────
    await check_content_title_unique(db, data.title, ctype)

    # ── 校验父级节点 ─────────────────────────────────────────────────
    if ctype == ContentType.EPISODE.value:
        if not data.parent_id:
            raise BusinessException(ErrorCode.EPISODE_REQUIRES_SERIES, get_msg("EPISODE_REQUIRES_SERIES"))
        parent = await _get_content_or_404(db, data.parent_id)
        if parent.content_type not in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
            raise BusinessException(ErrorCode.EPISODE_PARENT_MUST_BE_SERIES, get_msg("EPISODE_PARENT_MUST_BE_SERIES"))
        # 校验集序号是否已存在
        if data.sequence is not None:
            existing = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == data.parent_id,
                        Content.content_type == ContentType.EPISODE.value,
                        Content.sequence == data.sequence,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if existing:
                raise BusinessException(ErrorCode.EPISODE_SEQUENCE_EXISTS, get_msg("EPISODE_SEQUENCE_EXISTS"))

    elif ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
        if data.parent_id and data.series_ordinal is not None:
            existing = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == data.parent_id,
                        Content.content_type.in_([ContentType.SERIES.value, ContentType.SEASON_SERIES.value]),
                        Content.series_ordinal == data.series_ordinal,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalar_one_or_none()
            if existing:
                raise BusinessException(ErrorCode.SERIES_ORDINAL_EXISTS, get_msg("SERIES_ORDINAL_EXISTS"))

    elif ctype == ContentType.SCHEDULE.value:
        if not data.parent_id:
            raise BusinessException(ErrorCode.SCHEDULE_REQUIRES_CHANNEL, get_msg("SCHEDULE_REQUIRES_CHANNEL"))
        parent = await _get_content_or_404(db, data.parent_id)
        if parent.content_type != ContentType.CHANNEL.value:
            raise BusinessException(ErrorCode.SCHEDULE_PARENT_MUST_BE_CHANNEL, get_msg("SCHEDULE_PARENT_MUST_BE_CHANNEL"))
        if data.begin_time is None or data.end_time is None:
            raise BusinessException(ErrorCode.START_TIME_BEFORE_END_TIME, get_msg("START_TIME_BEFORE_END_TIME"))
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

    # ── 创建主节点 ───────────────────────────────────────────────────
    series_type_val: Optional[int] = None
    if ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
        series_type_val = data.series_type if data.series_type else 1
    elif ctype == ContentType.SEASON.value:
        series_type_val = 3  # 总季

    main_content = Content(
        content_type=ctype,
        title=data.title,
        status=ContentStatus.NONE.value,
        parent_id=data.parent_id,
        series_type=series_type_val,
        sequence=data.sequence if ctype == ContentType.EPISODE.value else None,
        series_ordinal=data.series_ordinal if ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value) else None,
        begin_time=data.begin_time if ctype == ContentType.SCHEDULE.value else None,
        end_time=data.end_time if ctype == ContentType.SCHEDULE.value else None,
    )
    db.add(main_content)
    await db.flush()  # 获取 main_content.id

    # ── 保存题材关联 ─────────────────────────────────────────
    if data.genre_ids:
        for gid in data.genre_ids:
            db.add(ContentGenre(content_id=main_content.id, genre_id=gid))

    main_content.external_id = str(main_content.id)

    # ── 自动创建子节点（SERIES → EPISODE）───────────────────────────
    if ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value) and data.volumn_count and data.volumn_count > 0:
        from app.internal.cms_biz_orchestration.models.episode_history import EpisodeHistory
        for seq in range(1, data.volumn_count + 1):
            ep = Content(
                content_type=ContentType.EPISODE.value,
                title=f"{data.title} E{seq:02d}",
                status=ContentStatus.NONE.value,
                parent_id=main_content.id,
                sequence=seq,
            )
            db.add(ep)
            await db.flush()
            ep.external_id = str(ep.id)

            # 记录 EpisodeHistory
            db.add(EpisodeHistory(
                parent_id=main_content.id,
                content_id=ep.id,
                content_name=ep.title,
                content_type=ContentType.EPISODE.value,
                series_ordinal=None,
                processed_by=processed_by or "system",
                processed_type="Add",
                created_by=None,
            ))

        # 记录 InjectSubContent 流程节点状态
        from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
        await complete_process_and_update_status(
            db,
            content_id=main_content.id,
            content_type=ctype,
            process_name="InjectSubContent",
            processed_by=processed_by or "system",
            info=f"批量创建 {data.volumn_count} 个 EPISODE 子节点",
        )

    # ── 自动创建子节点（SEASON → SERIES → EPISODE）──────────────────
    elif ctype == ContentType.SEASON.value and data.season_details:
        for detail in data.season_details:
            series_child = Content(
                content_type=ContentType.SEASON_SERIES.value,
                title=f"{data.title} S{detail.series_ordinal:02d}",
                status=ContentStatus.NONE.value,
                parent_id=main_content.id,
                series_type=2,
                series_ordinal=detail.series_ordinal,
            )
            db.add(series_child)
            await db.flush()

            series_child.external_id = str(series_child.id)

            for seq in range(1, detail.episode_count + 1):
                ep = Content(
                    content_type=ContentType.EPISODE.value,
                    title=f"{data.title} S{detail.series_ordinal:02d}E{seq:02d}",
                    status=ContentStatus.NONE.value,
                    parent_id=series_child.id,
                    sequence=seq,
                )
                db.add(ep)
                await db.flush()
                ep.external_id = str(ep.id)

            # 为 SEASON_SERIES 子节点记录 InjectSubContent 流程状态并更新内容状态
            # 单季下已创建 EPISODE 子节点，状态应从 None 变为 InProgress
            from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
            await complete_process_and_update_status(
                db,
                content_id=series_child.id,
                content_type=ContentType.SEASON_SERIES.value,
                process_name="InjectSubContent",
                processed_by=processed_by or "system",
                info=f"批量创建 {detail.episode_count} 个 EPISODE 子节点",
            )
        
        # 记录 SEASON 自身的 InjectSubContent 流程节点状态
        from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
        await complete_process_and_update_status(
            db,
            content_id=main_content.id,
            content_type=ctype,
            process_name="InjectSubContent",
            processed_by=processed_by or "system",
            info=f"批量创建 {len(data.season_details)} 个 SEASON_SERIES 子节点",
        )

    await db.flush()

    # ── 保存自定义标签关联 ─────────────────────────────────────────
    if data.custom_tag_ids:
        for tag_id in data.custom_tag_ids:
            assoc = ContentCustomTag(content_id=main_content.id, custom_tag_id=tag_id)
            db.add(assoc)

        # 将自定义标签传播到自动创建的子节点
        if ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
            child_ids = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == main_content.id,
                        Content.content_type == ContentType.EPISODE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for child_id in child_ids:
                for tag_id in data.custom_tag_ids:
                    assoc = ContentCustomTag(content_id=child_id, custom_tag_id=tag_id)
                    db.add(assoc)

        elif ctype == ContentType.SEASON.value:
            child_ids = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == main_content.id,
                        Content.content_type == ContentType.SEASON_SERIES.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for child_id in child_ids:
                for tag_id in data.custom_tag_ids:
                    assoc = ContentCustomTag(content_id=child_id, custom_tag_id=tag_id)
                    db.add(assoc)
            # 传播到 EPISODE 孙节点
            grandchild_ids = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id.in_(child_ids),
                        Content.content_type == ContentType.EPISODE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for grandchild_id in grandchild_ids:
                for tag_id in data.custom_tag_ids:
                    assoc = ContentCustomTag(content_id=grandchild_id, custom_tag_id=tag_id)
                    db.add(assoc)

    # ── 将题材关联传播到自动创建的子节点 ──────────────────────────────
    if data.genre_ids:
        if ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
            child_ids = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == main_content.id,
                        Content.content_type == ContentType.EPISODE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for child_id in child_ids:
                for gid in data.genre_ids:
                    db.add(ContentGenre(content_id=child_id, genre_id=gid))

        elif ctype == ContentType.SEASON.value:
            child_ids = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == main_content.id,
                        Content.content_type == ContentType.SEASON_SERIES.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for child_id in child_ids:
                for gid in data.genre_ids:
                    db.add(ContentGenre(content_id=child_id, genre_id=gid))
            # 传播到 EPISODE 孙节点
            grandchild_ids = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id.in_(child_ids),
                        Content.content_type == ContentType.EPISODE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for grandchild_id in grandchild_ids:
                for gid in data.genre_ids:
                    db.add(ContentGenre(content_id=grandchild_id, genre_id=gid))

    await db.commit()
    await db.refresh(main_content)

    # ── 为子内容注入记录流程状态 ──────────────────────────────────
    # 当创建 EPISODE（作为 SERIES 的子节点）或 SERIES（作为 SEASON 的子节点）时，
    # 需要记录父节点的 InjectSubContent 流程状态
    if ctype == ContentType.EPISODE.value and data.parent_id:
        # 检查父节点是否为 SERIES
        parent = await _get_content_or_404(db, data.parent_id)
        if parent.content_type in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
            from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status, rollback_after_published_edit
            # 已发布/准备发布等状态的父级被编辑后，先回滚状态并重置审核流程
            await rollback_after_published_edit(
                db,
                content_id=parent.id,
                content_type=parent.content_type,
                edited_by=processed_by or "system",
                edit_info=f"添加单集: {data.title}",
            )
            # 再记录 InjectSubContent 流程节点完成
            await complete_process_and_update_status(
                db,
                content_id=data.parent_id,
                content_type=parent.content_type,
                process_name="InjectSubContent",
                processed_by=processed_by or "system",
                info=f"注入 EPISODE 子节点: {data.title}",
            )

    elif ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value) and data.parent_id:
        parent = await _get_content_or_404(db, data.parent_id)
        if parent.content_type == ContentType.SEASON.value:
            from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status, rollback_after_published_edit
            # 已发布/准备发布等状态的父级被编辑后，先回滚状态并重置审核流程
            await rollback_after_published_edit(
                db,
                content_id=parent.id,
                content_type=parent.content_type,
                edited_by=processed_by or "system",
                edit_info=f"添加子季: {data.title}",
            )
            # 再记录 InjectSubContent 流程节点完成
            await complete_process_and_update_status(
                db,
                content_id=data.parent_id,
                content_type=parent.content_type,
                process_name="InjectSubContent",
                processed_by=processed_by or "system",
                info=f"注入 {ctype} 子节点: {data.title}",
            )

    # 自动创建 arrangement 任务
    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(
        db, main_content.id, assignee_id=data.assignee_id, processed_by="system"
    )
    
    # 为自动创建的子内容也创建 arrangement 任务
    if ctype in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value) and data.volumn_count and data.volumn_count > 0:
        child_contents = (
            await db.execute(
                select(Content).where(
                    Content.parent_id == main_content.id,
                    Content.content_type == ContentType.EPISODE.value,
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalars().all()
        for child in child_contents:
            await task_service.create_arrangement_task(
                db, child.id, assignee_id=data.assignee_id, processed_by="system"
            )
    
    elif ctype == ContentType.SEASON.value and data.season_details:
        series_children = (
            await db.execute(
                select(Content).where(
                    Content.parent_id == main_content.id,
                    Content.content_type == ContentType.SEASON_SERIES.value,
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalars().all()
        for series_child in series_children:
            await task_service.create_arrangement_task(
                db, series_child.id, assignee_id=data.assignee_id, processed_by="system"
            )
            
            # 为 EPISODE 孙内容创建任务
            episode_children = (
                await db.execute(
                    select(Content).where(
                        Content.parent_id == series_child.id,
                        Content.content_type == ContentType.EPISODE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            for episode_child in episode_children:
                await task_service.create_arrangement_task(
                    db, episode_child.id, assignee_id=data.assignee_id, processed_by="system"
                )
    
    # ── 继承父内容许可证 ─────────────────────────────────────────
    # 新建子内容时，自动继承父内容的许可证关联（跳过已存在关联，含软删除记录）
    if data.parent_id and ctype in (
        ContentType.SEASON_SERIES.value,
        ContentType.SERIES.value,
        ContentType.EPISODE.value,
    ):
        parent_license_ids = (
            await db.execute(
                select(LicenseContent.license_id).where(
                    LicenseContent.content_id == data.parent_id,
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        if parent_license_ids:
            existing_license_ids = set(
                (
                    await db.execute(
                        select(LicenseContent.license_id).where(
                            LicenseContent.content_id == main_content.id,
                        )
                    )
                ).scalars().all()
            )
            added_license_ids: list[int] = []
            for lid in parent_license_ids:
                if lid in existing_license_ids:
                    continue
                db.add(LicenseContent(license_id=lid, content_id=main_content.id))
                added_license_ids.append(lid)
            if added_license_ids:
                logger.info(
                    f"继承父内容 #{data.parent_id} 许可证: {added_license_ids} → 子内容 #{main_content.id}"
                )

    await db.commit()

    logger.info(f"创建内容成功: id={main_content.id}, type={ctype}, title={data.title}")
    return await _build_item(db, main_content)


async def update_content(db: AsyncSession, content_id: int, data: ContentUpdate) -> ContentListItem:
    """
    更新内容基本信息。

    输入：
        content_id  内容 id
        data        ContentUpdate（所有字段均可选）
    输出：
        更新后的 ContentListItem
    业务规则：
        - 不支持修改 content_type
    """
    logger.info(f"update_content 入参: content_id={content_id}, data={data}")
    c = await _get_content_or_404(db, content_id)

    # ── 校验内容名称唯一性（同一 content_type 下不能重名）─────────
    if data.title is not None and data.title != c.title:
        await check_content_title_unique(db, data.title, c.content_type, exclude_id=content_id)
        c.title = data.title
    if data.parent_id is not None:
        c.parent_id = data.parent_id
    if data.sequence is not None:
        c.sequence = data.sequence
    if data.begin_time is not None:
        c.begin_time = data.begin_time
    if data.end_time is not None:
        c.end_time = data.end_time
    if data.cutv_enable is not None:
        c.cutv_enable = data.cutv_enable
    if data.is_archived is not None:
        c.is_archived = data.is_archived

    # ── 校验 EPISODE 父级及集序号唯一性 ─────────────────────────────
    if c.content_type == ContentType.EPISODE.value:
        target_parent_id = data.parent_id if data.parent_id is not None else c.parent_id
        target_sequence = data.sequence if data.sequence is not None else c.sequence
        if target_parent_id is not None:
            parent = await _get_content_or_404(db, target_parent_id)
            if parent.content_type not in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value):
                raise BusinessException(ErrorCode.EPISODE_PARENT_MUST_BE_SERIES, get_msg("EPISODE_PARENT_MUST_BE_SERIES"))
        if target_sequence is not None and target_parent_id is not None:
            existing = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == target_parent_id,
                        Content.content_type == ContentType.EPISODE.value,
                        Content.sequence == target_sequence,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                        Content.id != content_id,
                    )
                )
            ).scalar_one_or_none()
            if existing:
                raise BusinessException(ErrorCode.EPISODE_SEQUENCE_EXISTS, get_msg("EPISODE_SEQUENCE_EXISTS"))

    # ── 校验 SCHEDULE 同频道时间段不冲突 ──────────────────────────────
    logger.info(
        f"[SCHEDULE_CONFLICT_CHECK] content_id={content_id} "
        f"content_type={c.content_type} data.begin_time={data.begin_time} "
        f"data.end_time={data.end_time} data.parent_id={data.parent_id} "
        f"c.begin_time={c.begin_time} c.end_time={c.end_time} c.parent_id={c.parent_id}"
    )
    if c.content_type == ContentType.SCHEDULE.value and (
        data.begin_time is not None or data.end_time is not None or data.parent_id is not None
    ):
        target_begin = data.begin_time if data.begin_time is not None else c.begin_time
        target_end = data.end_time if data.end_time is not None else c.end_time
        target_parent_id = data.parent_id if data.parent_id is not None else c.parent_id
        logger.info(
            f"[SCHEDULE_CONFLICT_CHECK] target_begin={target_begin} target_end={target_end} "
            f"target_parent_id={target_parent_id}"
        )
        if target_begin is not None and target_end is not None and target_parent_id is not None:
            if target_begin >= target_end:
                raise BusinessException(ErrorCode.START_TIME_BEFORE_END_TIME, get_msg("START_TIME_BEFORE_END_TIME"))
            overlap = (
                await db.execute(
                    select(Content.id).where(
                        Content.content_type == ContentType.SCHEDULE.value,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                        Content.parent_id == target_parent_id,
                        Content.begin_time < target_end,
                        Content.end_time > target_begin,
                        Content.id != content_id,
                    ).limit(1)
                )
            ).scalar_one_or_none()
            logger.info(f"[SCHEDULE_CONFLICT_CHECK] overlap_id={overlap}")
            if overlap:
                raise BusinessException(ErrorCode.SCHEDULE_TIME_CONFLICT, get_msg("SCHEDULE_TIME_CONFLICT"))

    # ── 更新自定义标签关联 ─────────────────────────────────────────
    if data.custom_tag_ids is not None:
        # 删除现有标签关联
        await db.execute(
            ContentCustomTag.__table__.delete().where(ContentCustomTag.content_id == content_id)
        )
        # 添加新的标签关联
        for tag_id in data.custom_tag_ids:
            assoc = ContentCustomTag(content_id=content_id, custom_tag_id=tag_id)
            db.add(assoc)

    # ── 更新题材关联 ─────────────────────────────────────────
    if data.genre_ids is not None:
        # 删除现有题材关联
        await db.execute(
            ContentGenre.__table__.delete().where(ContentGenre.content_id == content_id)
        )
        # 添加新的题材关联
        for gid in data.genre_ids:
            db.add(ContentGenre(content_id=content_id, genre_id=gid))

    await db.commit()
    await db.refresh(c)
    return await _build_item(db, c)


async def delete_content(db: AsyncSession, content_id: int) -> None:
    """
    软删除内容(设置 is_discarded=True)。

    输入:content_id
    业务规则:
        - 存在子内容时不允许删除,需先删除子内容
        - 删除归档内容时,若原节目单无其他未删除归档内容,则重置节目单 is_archived=False
    """
    logger.info(f"delete_content 入参: content_id={content_id}")
    c = await _get_content_or_404(db, content_id)
    
    if c.status in ("Published", "Publishing"):
        raise BusinessException(ErrorCode.CONTENT_PUBLISHED_CANNOT_DISCARD, get_msg("CONTENT_PUBLISHED_CANNOT_DISCARD"))
    
    child_count = (
        await db.execute(
            select(func.count()).select_from(Content).where(
                Content.parent_id == content_id,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one()
    
    if child_count > 0:
        raise BusinessException(ErrorCode.CONTENT_HAS_CHILDREN, get_msg("CONTENT_HAS_CHILDREN"))
    
    # 若删除的是归档内容,检查是否需要重置原节目单的 is_archived
    source_schedule_id = c.source_schedule_id
    is_archived_content = c.is_archived
    
    logger.info(f"[归档删除调试] content_id={content_id}, source_schedule_id={source_schedule_id}, is_archived={is_archived_content}")
    
    c.previous_status = c.status
    c.is_discarded = True

    # 自动解绑许可证
    await db.execute(
        update(LicenseContent)
        .where(LicenseContent.content_id == content_id, LicenseContent.is_deleted.is_(False))
        .values(is_deleted=True)
    )

    # 自动解绑服务包
    await db.execute(
        update(ContentPackage)
        .where(ContentPackage.content_id == content_id, ContentPackage.is_deleted.is_(False))
        .values(is_deleted=True)
    )

    # 自动解绑栏目
    await db.execute(
        update(ContentCategory)
        .where(ContentCategory.content_id == content_id, ContentCategory.is_deleted.is_(False))
        .values(is_deleted=True)
    )

    # 归档内容删除后,检查原节目单是否还有其他未删除的归档内容
    if is_archived_content and source_schedule_id:
        from sqlalchemy import update as sql_update

        # 注意：不清空节目单 schedule_metadata.program_id。
        # program_id 是 cutv_enable=true 时的条件必填字段（0076 校验规则），
        # 且原值由归档流程自动回写、用户无法手工补填——清空会导致节目单
        # 元数据永久红叉且弹窗必填校验拦截保存（流程卡死）。
        # 保留原值符合"删除归档节目不影响原始节目单"的产品语义；
        # 重新归档时 archive_schedule 会自动改指向新归档节目。

        remaining_archive_count = (
            await db.execute(
                select(func.count()).select_from(Content).where(
                    Content.source_schedule_id == source_schedule_id,
                    Content.is_archived.is_(True),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalar_one()
        
        logger.info(f"[归档删除调试] 节目单 {source_schedule_id} 剩余归档内容数量: {remaining_archive_count}")
        
        # 若无其他归档内容,重置节目单的 is_archived=False
        if remaining_archive_count == 0:
            await db.execute(
                sql_update(Content)
                .where(Content.id == source_schedule_id)
                .values(is_archived=False)
            )
            logger.info(f"[归档删除调试] 已重置节目单 {source_schedule_id} 的 is_archived=False")

    # 删除子内容后同步父级状态（EPISODE → SERIES/SEASON_SERIES → SEASON）
    if c.parent_id is not None:
        await ContentStatusService.sync_parent_status(
            db, c.id, c.content_type
        )

    await db.commit()


async def batch_delete_contents(db: AsyncSession, content_ids: list[int]) -> int:
    """
    批量软删除内容(设置 is_discarded=True)。

    输入:content_ids
    业务规则:
        - 存在子内容的内容不允许删除,跳过
        - 删除归档内容时,若原节目单无其他未删除归档内容,则重置节目单 is_archived=False
    返回:成功删除的数量
    """
    logger.info(f"batch_delete_contents 入参: content_ids={content_ids}")
    deleted_count = 0
    schedule_ids_to_check: set[int] = set()  # 收集需要检查的节目单ID
    
    for content_id in content_ids:
        try:
            c = await _get_content_or_404(db, content_id)
            
            if c.status == "Published":
                logger.warning(f"batch_delete_contents: content_id={content_id} is published, skip")
                continue
            
            child_count = (
                await db.execute(
                    select(func.count()).select_from(Content).where(
                        Content.parent_id == content_id,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalar_one()
            
            if child_count > 0:
                logger.warning(f"batch_delete_contents: content_id={content_id} has {child_count} children, skip")
                continue
            
            # 记录归档内容的原节目单ID
            if c.is_archived and c.source_schedule_id:
                schedule_ids_to_check.add(c.source_schedule_id)

            c.previous_status = c.status
            c.is_discarded = True

            # 自动解绑许可证
            await db.execute(
                update(LicenseContent)
                .where(LicenseContent.content_id == content_id, LicenseContent.is_deleted.is_(False))
                .values(is_deleted=True)
            )

            # 自动解绑服务包
            await db.execute(
                update(ContentPackage)
                .where(ContentPackage.content_id == content_id, ContentPackage.is_deleted.is_(False))
                .values(is_deleted=True)
            )

            # 自动解绑栏目
            await db.execute(
                update(ContentCategory)
                .where(ContentCategory.content_id == content_id, ContentCategory.is_deleted.is_(False))
                .values(is_deleted=True)
            )

            deleted_count += 1
        except NotFoundException:
            logger.warning(f"batch_delete_contents: content_id={content_id} not found, skip")
            continue
    
    # 批量检查并重置节目单的 is_archived
    from sqlalchemy import update as sql_update
    for schedule_id in schedule_ids_to_check:
        # 注意：不清空节目单 schedule_metadata.program_id（同 delete_content，
        # program_id 为条件必填且由归档自动回写，清空会导致节目单元数据永久红叉）

        remaining_archive_count = (
            await db.execute(
                select(func.count()).select_from(Content).where(
                    Content.source_schedule_id == schedule_id,
                    Content.is_archived.is_(True),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalar_one()
        
        if remaining_archive_count == 0:
            await db.execute(
                sql_update(Content)
                .where(Content.id == schedule_id)
                .values(is_archived=False)
            )
            logger.info(f"批量删除归档内容后,重置节目单 {schedule_id} 的 is_archived=False")

    await db.commit()
    logger.info(f"batch_delete_contents 完成: deleted_count={deleted_count}")
    return deleted_count


async def _soft_delete_tree(db: AsyncSession, content_id: int) -> None:
    """递归软删除 content_id 及其所有子孙节点。"""
    children = (
        await db.execute(
            select(Content).where(Content.parent_id == content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
        )
    ).scalars().all()
    for child in children:
        await _soft_delete_tree(db, child.id)
        child.is_deleted = True

    main = (await db.execute(select(Content).where(Content.id == content_id))).scalar_one_or_none()
    if main:
        main.is_deleted = True


def _get_entity_type_for_content(content_type: str) -> str:
    """根据内容类型获取 Picture 的 entity_type（SERIES/SEASON_SERIES/SEASON 统一为 series）。"""
    mapping = {
        "MOVIE": "program",
        "EPISODE": "program",
        "SERIES": "series",
        "SEASON_SERIES": "series",
        "SEASON": "series",
        "CHANNEL": "channel",
        "SCHEDULE": "schedule",
    }
    return mapping.get(content_type, "content")


async def get_without_license_count(db: AsyncSession, current_user: Optional[User] = None) -> int:
    """
    统计当前未关联任何许可证的内容数量（用于"Without License"快捷按钮）。

    输出：整数
    """
    logger.info(f"get_without_license_count 入参: 无")
    # 使用 NOT EXISTS 替代 NOT IN，避免子查询返回 NULL 时的问题
    sub_lc_exists = select(LicenseContent).where(
        LicenseContent.content_id == Content.id,
        LicenseContent.is_deleted.is_(False),
    )
    base_query = select(Content).where(
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
        ~sub_lc_exists.exists(),
    )
    # 数据权限过滤：admin 不过滤；其他用户只统计其有权限看到的内容
    base_query = await apply_content_data_auth(db, current_user, base_query)
    result = await db.execute(
        select(func.count()).select_from(base_query.subquery())
    )
    return result.scalar_one()


async def get_content_licenses(db: AsyncSession, content_id: int) -> list[ContentLicenseRef]:
    """
    查询内容已关联的许可证列表。

    输入：content_id
    输出：ContentLicenseRef 列表
    """
    logger.info(f"get_content_licenses 入参: content_id={content_id}")
    await _get_content_or_404(db, content_id)

    lic_ids = (
        await db.execute(
            select(LicenseContent.license_id).where(
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
            )
        )
    ).scalars().all()

    if not lic_ids:
        return []

    from app.internal.cms_biz_scp.models.trade import Contract, Provider, LicensePlatform
    from app.internal.cms_biz_system.models.dict import DictNode

    # 加载 ServiceType 字典，用于将编码翻译为名称
    service_type_root = (
        await db.execute(
            select(DictNode).where(
                DictNode.code == "ServiceType",
                DictNode.parent_id.is_(None),
                DictNode.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    service_type_map: dict[str, str] = {}
    if service_type_root:
        st_children = (
            await db.execute(
                select(DictNode).where(
                    DictNode.parent_id == service_type_root.id,
                    DictNode.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        for child in st_children:
            service_type_map[child.code] = child.name

    # 批量查询所有 License（selectin 自动加载 contract → provider 和 platforms）
    # 替代原来逐个 license 循环查询的 N+1 模式
    licenses = (
        await db.execute(
            select(License)
            .where(License.id.in_(lic_ids), License.is_deleted.is_(False))
            .order_by(License.id)
        )
    ).scalars().all()

    result: list[ContentLicenseRef] = []
    for lic_row in licenses:
        contract = lic_row.contract
        provider_name = ""
        provider_id = 0
        if contract:
            provider_id = contract.provider_id
            if contract.provider:
                provider_name = contract.provider.name

        # platforms 通过 selectin 自动加载，过滤已删除的
        platform_rows = [p for p in (lic_row.platforms or []) if not p.is_deleted]
        platforms = [
            {"platform": p.platform, "ad_rights": p.ad_rights}
            for p in platform_rows
        ] if platform_rows else None

        result.append(ContentLicenseRef(
            id=lic_row.id,
            name=lic_row.name,
            contract_id=lic_row.contract_id,
            contract_name=contract.name if contract else "",
            provider_id=provider_id,
            provider_name=provider_name,
            service_type=lic_row.service_type,
            service_type_name=service_type_map.get(lic_row.service_type),
            start_date=str(lic_row.start_date) if lic_row.start_date else None,
            end_date=str(lic_row.end_date) if lic_row.end_date else None,
            platforms=platforms,
        ))

    return result


async def get_series_simple(db: AsyncSession, current_user: Optional[User] = None) -> list[ContentSimpleItem]:
    """
    获取所有 SERIES 内容简要列表（用于 EPISODE 的父级下拉选择）。

    输出：ContentSimpleItem 列表
    """
    logger.info(f"get_series_simple 入参: 无")
    query = select(Content).where(
        Content.content_type.in_([ContentType.SERIES.value, ContentType.SEASON_SERIES.value]),
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    query = await apply_content_data_auth(db, current_user, query)
    rows = (
        await db.execute(query.order_by(Content.title.asc()))
    ).scalars().all()
    return [ContentSimpleItem(id=c.id, content_type=c.content_type, title=c.title) for c in rows]


async def get_seasons_simple(db: AsyncSession, current_user: Optional[User] = None) -> list[ContentSimpleItem]:
    """
    获取所有 SEASON 内容简要列表（用于 SEASON_SERIES 的父级下拉选择）。

    输出：ContentSimpleItem 列表
    """
    logger.info(f"get_seasons_simple 入参: 无")
    query = select(Content).where(
        Content.content_type == ContentType.SEASON.value,
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    query = await apply_content_data_auth(db, current_user, query)
    rows = (
        await db.execute(query.order_by(Content.title.asc()))
    ).scalars().all()
    return [ContentSimpleItem(id=c.id, content_type=c.content_type, title=c.title) for c in rows]


async def get_channels_simple(db: AsyncSession, current_user: Optional[User] = None) -> list[ContentSimpleItem]:
    """
    获取所有 CHANNEL 内容简要列表（用于 SCHEDULE 的频道下拉选择）。

    输出：ContentSimpleItem 列表
    """
    logger.info(f"get_channels_simple 入参: 无")
    query = select(Content).where(
        Content.content_type == ContentType.CHANNEL.value,
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    # 数据权限过滤：admin 不过滤；其他用户仅看到自己有权限的 CHANNEL
    query = await apply_content_data_auth(db, current_user, query)
    rows = (
        await db.execute(query.order_by(Content.title.asc()))
    ).scalars().all()
    return [ContentSimpleItem(id=c.id, content_type=c.content_type, title=c.title) for c in rows]


# ─── VOD 点播管理视角 ──────────────────────────────────────────────────

_VOD_TYPES = [ContentType.MOVIE.value, ContentType.SEASON.value, ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.EPISODE.value]


async def _build_vod_item(db: AsyncSession, c: Content, meta_name: Optional[str] = None) -> VodContentListItem:
    """将 ORM Content 转换为 VOD 列表响应，附加服务包名称和供应商名称。"""
    genre_ids, genre_name = await _get_genre_names(db, c.id)

    # 关联服务包名称列表
    pkg_ids = (
        await db.execute(
            select(ContentPackage.package_id).where(ContentPackage.content_id == c.id)
        )
    ).scalars().all()
    package_names: list[str] = []
    if pkg_ids:
        names = (
            await db.execute(
                select(Package.name).where(
                    Package.id.in_(pkg_ids),
                    Package.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        package_names = list(names)

    # 关联供应商名称列表（通过 license_content → license → contract → provider）
    lic_ids = (
        await db.execute(
            select(LicenseContent.license_id).where(
                LicenseContent.content_id == c.id,
                LicenseContent.is_deleted.is_(False),
            )
        )
    ).scalars().all()
    provider_names: list[str] = []
    license_start: Optional[str] = None
    license_end: Optional[str] = None
    if lic_ids:
        lics = (
            await db.execute(
                select(License).where(
                    License.id.in_(lic_ids),
                    License.is_deleted.is_(False),
                )
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
        if starts:
            license_start = str(min(starts))
        if ends:
            license_end = str(max(ends))

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
    elif c.content_type in (ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.SEASON.value):
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

    # 查询栏目名称列表
    category_ids = (
        await db.execute(
            select(ContentCategory.category_id).where(ContentCategory.content_id == c.id)
        )
    ).scalars().all()
    category_name: Optional[str] = None
    if category_ids:
        cat_names = (
            await db.execute(
                select(Category.name).where(
                    Category.id.in_(category_ids),
                )
            )
        ).scalars().all()
        if cat_names:
            category_name = ", ".join(cat_names)

    # 查询海报URL（取第一张非删除的海报）
    poster_url: Optional[str] = None
    entity_type = _get_entity_type_for_content(c.content_type)
    # 同时匹配小写和大写的 entity_type
    entity_types = [entity_type, entity_type.capitalize()]
    pic = (
        await db.execute(
            select(Picture.file_path, Picture.relative_path)
            .where(
                Picture.entity_type.in_(entity_types),
                Picture.entity_id == c.id,
                Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
            )
            .order_by(Picture.created_at)
            .limit(1)
        )
    ).one_or_none()
    if pic:
        poster_url = storage_service.get_file_url(pic.file_path, pic.relative_path)

    # 查询发布日期和下架日期（从 publish_task 表获取）
    publish_date: Optional[str] = None
    unpublish_date: Optional[str] = None

    # 查询发布任务，取最早的 publish_time
    publish_task_result = (
        await db.execute(
            select(PublishTask.publish_time)
            .where(
                PublishTask.entity_type == "Content",
                PublishTask.entity_id == c.id,
                PublishTask.publish_time.is_not(None),
            )
            .order_by(PublishTask.publish_time.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if publish_task_result:
        publish_date = publish_task_result.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S")

    # 查询下架任务，取最新的 unpublish_time
    unpublish_task_result = (
        await db.execute(
            select(PublishTask.unpublish_time)
            .where(
                PublishTask.entity_type == "Content",
                PublishTask.entity_id == c.id,
                PublishTask.unpublish_time.is_not(None),
            )
            .order_by(PublishTask.unpublish_time.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if unpublish_task_result:
        unpublish_date = unpublish_task_result.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S")

    return VodContentListItem(
        id=c.id,
        content_type=c.content_type,
        title=c.title,
        status=c.status,
        genre_ids=genre_ids,
        genre_name=genre_name,
        type_name=type_name,
        category_name=category_name,
        custom_tag_names=(await _get_custom_tags(db, c.id))[1],
        unpublish_date=unpublish_date,
        publish_date=publish_date,
        poster_url=poster_url,
        package_names=package_names,
        provider_names=provider_names,
        license_start=license_start,
        license_end=license_end,
        created_at=c.created_at,
        is_discarded=c.is_discarded,
    )


async def list_vod_contents(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    title: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    statuses: Optional[list[str]] = None,
    genre_ids: Optional[list[int]] = None,
    custom_tag_ids: Optional[list[int]] = None,
    deleted: Optional[str] = None,
    type_ids: Optional[list[int]] = None,
    category_name: Optional[str] = None,
    package_ids: Optional[list[int]] = None,
    provider_ids: Optional[list[int]] = None,
    license_start_from: Optional[str] = None,
    license_start_to: Optional[str] = None,
    license_end_from: Optional[str] = None,
    license_end_to: Optional[str] = None,
    unpublish_from: Optional[str] = None,
    unpublish_to: Optional[str] = None,
    publish_from: Optional[str] = None,
    publish_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
    current_user: Optional[User] = None,
) -> PaginatedResponse[VodContentListItem]:
    """
    查询点播内容列表（仅 MOVIE/SEASON/SERIES/EPISODE 类型，分页）。

    输入参数：
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
        provider_ids        供应商 id 列表（通过 license_content→license→contract 关联）
        license_start_from  许可证开始日期范围下限（YYYY-MM-DD）
        license_start_to    许可证开始日期范围上限
        license_end_from    许可证结束日期范围下限
        license_end_to      许可证结束日期范围上限
        unpublish_from      下架日期范围下限（YYYY-MM-DD）
        unpublish_to        下架日期范围上限
        publish_from        发布日期范围下限（YYYY-MM-DD）
        publish_to          发布日期范围上限

    输出：
        PaginatedResponse[VodContentListItem]

    过滤规则：
        - content_type IN (MOVIE/SEASON/SERIES/EPISODE)
        - is_archived = false（仅普通内容，归档内容由归档管理模块负责）
    """
    logger.info(f"list_vod_contents 入参: page={page}, page_size={page_size}, title={title}, content_types={content_types}, statuses={statuses}, genre_ids={genre_ids}, custom_tag_ids={custom_tag_ids}, deleted={deleted}, type_ids={type_ids}, category_name={category_name}, package_ids={package_ids}, provider_ids={provider_ids}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, unpublish_from={unpublish_from}, unpublish_to={unpublish_to}, publish_from={publish_from}, publish_to={publish_to}, sort_by={sort_by}, sort_order={sort_order}")
    allowed = set(_VOD_TYPES)
    if content_types:
        allowed = allowed & set(content_types)
    effective_types = list(allowed) if allowed else _VOD_TYPES

    if deleted == 'YES':
        query = select(Content).where(
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(True),
            Content.is_archived.is_(False),
            Content.content_type.in_(effective_types),
        )
    else:
        query = select(Content).where(
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),
            Content.is_archived.is_(False),
            Content.content_type.in_(effective_types),
        )
    query = await apply_content_data_auth(db, current_user, query)

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if statuses:
        query = query.where(Content.status.in_(statuses))
    if genre_ids:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id.in_(genre_ids))
        query = query.where(Content.id.in_(subq))

    if custom_tag_ids:
        content_ids_from_tags = (
            await db.execute(
                select(ContentCustomTag.content_id).where(
                    ContentCustomTag.custom_tag_id.in_(custom_tag_ids)
                )
            )
        ).scalars().all()
        query = query.where(Content.id.in_(content_ids_from_tags))

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

    if category_name:
        category_ids = (
            await db.execute(
                select(Category.id).where(
                    Category.name.ilike(f"%{category_name}%"),
                    Category.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        if category_ids:
            content_ids_from_cat = (
                await db.execute(
                    select(ContentCategory.content_id).where(
                        ContentCategory.category_id.in_(category_ids)
                    )
                )
            ).scalars().all()
            query = query.where(Content.id.in_(content_ids_from_cat))
        else:
            return PaginatedResponse(total=0, page=page, page_size=page_size, items=[])

    # 供应商过滤：找出该供应商下的所有许可证 id，再找关联的 content id
    if provider_ids:
        contract_ids = (
            await db.execute(
                select(Contract.id).where(
                    Contract.provider_id.in_(provider_ids),
                    Contract.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        if contract_ids:
            lic_ids = (
                await db.execute(
                    select(License.id).where(
                        License.contract_id.in_(contract_ids),
                        License.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
            content_ids_from_provider = (
                await db.execute(
                    select(LicenseContent.content_id).where(
                        LicenseContent.license_id.in_(lic_ids),
                        LicenseContent.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
            query = query.where(Content.id.in_(content_ids_from_provider))
        else:
            # 该供应商下无合同，结果为空
            return PaginatedResponse(total=0, page=page, page_size=page_size, items=[])

    if package_ids:
        content_ids_from_pkg = (
            await db.execute(
                select(ContentPackage.content_id).where(
                    ContentPackage.package_id.in_(package_ids)
                )
            )
        ).scalars().all()
        query = query.where(Content.id.in_(content_ids_from_pkg))

    # 许可证日期过滤
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

        content_ids_from_lic = (
            await db.execute(
                select(LicenseContent.content_id).where(
                    LicenseContent.license_id.in_(lic_query),
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        query = query.where(Content.id.in_(content_ids_from_lic))

    # 发布日期范围过滤：通过 PublishTask.publish_time 查询
    if publish_from or publish_to:
        pub_query = select(PublishTask.entity_id).where(
            PublishTask.entity_type == 'Content',
            PublishTask.task_type == 'publish',
            PublishTask.status == 'success',
            PublishTask.is_deleted.is_(False),
        )
        if publish_from:
            pub_query = pub_query.where(PublishTask.publish_time >= datetime.fromisoformat(publish_from))
        if publish_to:
            pub_query = pub_query.where(PublishTask.publish_time <= datetime.fromisoformat(publish_to).replace(hour=23, minute=59, second=59))
        query = query.where(Content.id.in_(pub_query))

    # 下架日期范围过滤：通过 PublishTask.unpublish_time 查询
    # 注意：CHANNEL/SCHEDULE 的下架时间记录在 task_type=publish 的记录中，而非独立的 unpublish 记录
    if unpublish_from or unpublish_to:
        unpub_query = select(PublishTask.entity_id).where(
            PublishTask.entity_type == 'Content',
            PublishTask.unpublish_time.is_not(None),
            PublishTask.is_deleted.is_(False),
        )
        if unpublish_from:
            unpub_query = unpub_query.where(PublishTask.unpublish_time >= datetime.fromisoformat(unpublish_from))
        if unpublish_to:
            unpub_query = unpub_query.where(PublishTask.unpublish_time <= datetime.fromisoformat(unpublish_to).replace(hour=23, minute=59, second=59))
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
    contents = (
        await db.execute(
            query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    # 批量查询元数据名称（MOVIE/EPISODE 查 program_metadata，SERIES/SEASON 查 series_metadata）
    content_ids = [c.id for c in contents]
    movie_episode_ids = [c.id for c in contents if c.content_type in (ContentType.MOVIE.value, ContentType.EPISODE.value)]
    series_season_ids = [c.id for c in contents if c.content_type in (ContentType.SERIES.value, ContentType.SEASON.value, ContentType.SEASON_SERIES.value)]

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

    items = [await _build_vod_item(db, c, meta_map.get(c.id)) for c in contents]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def _get_optional_node_codes(db: AsyncSession, content: Content) -> set[str]:
    """查询当前内容所属模块下，配置为非必填（mandatory=False）的节点编码集合。

    用于发布后兜底逻辑：可选节点不打勾，按实际数据状态显示。
    归档内容根据 content_type 走 ARCHIVED_MOVIE / ARCHIVED_EPISODE 配置分支。
    查询失败时返回空集（视所有节点为必填，等同原兜底行为）。
    """
    from app.internal.cms_biz_flow.repositories.workflow_config_repo import (
        get_published_workflow_by_belonging,
        list_workflow_nodes,
    )

    if getattr(content, "is_archived", False):
        if content.content_type == "EPISODE":
            belonging = "ARCHIVED_EPISODE"
        else:
            belonging = "ARCHIVED_MOVIE"
    else:
        belonging = content.content_type

    try:
        config = await get_published_workflow_by_belonging(db, belonging)
        if not config:
            return set()
        nodes = await list_workflow_nodes(db, config.id)
        return {n.node_code for n in nodes if not n.mandatory}
    except Exception as e:
        logger.warning(
            "查询可选节点配置失败 content_id={} belonging={}: {}",
            content.id, belonging, e,
        )
        return set()


async def get_node_completion_status(
    db: AsyncSession,
    content_id: int,
) -> dict[str, dict]:
    """
    基于实际数据关系判断各流程节点的完成状态。

    返回 {node_code: {"completed": bool, "warning": bool, "detail": str}}
    """
    from app.internal.cms_biz_orchestration.models.movie import Movie
    from app.internal.cms_biz_orchestration.models.cast_role_map import CastRoleMap
    from app.internal.cms_biz_package.models.package import PhysicalChannel
    from app.internal.cms_biz_orchestration.models.content_process import ContentProcess

    content = (await db.execute(
        select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
    )).scalar_one_or_none()
    if not content:
        return {}

    ct = content.content_type
    result: dict[str, dict] = {}

    # ── Materials: 检查是否有正片 Movie（movie_type=1） ──
    movie_count = 0
    if ct in ("MOVIE", "EPISODE"):
        movie_count = (await db.execute(
            select(func.count()).select_from(Movie).where(
                Movie.content_id == content_id,
                Movie.is_deleted.is_(False),
                Movie.movie_type == 1,
            )
        )).scalar() or 0
    result["Materials"] = {
        "completed": movie_count > 0,
        "warning": False,
        "detail": f"{movie_count} movie(s)" if movie_count else "No movies",
    }

    # ── Metadata: 只要存在任意 Passed 记录即绿勾 ──
    # 业务规则：同步/导入过来的数据无 Passed 记录 → 按必填字段实时校验；
    # 一旦通过页面编辑（metadata_service）/归档弹窗/其他入口写入了 Passed 记录，
    # 即视为用户已确认完成，overview 显示绿勾，不再回退到实时校验。
    # 这样避免了"最新一条是 Pending，前面有 Passed"时 overview 误红叉。
    has_meta_passed = (await db.execute(
        select(func.count()).select_from(ContentProcess).where(
            ContentProcess.content_id == content_id,
            ContentProcess.node_code == "Metadata",
            ContentProcess.status == "Passed",
            ContentProcess.is_deleted.is_(False),
        )
    )).scalar() or 0
    if has_meta_passed:
        result["Metadata"] = {
            "completed": True,
            "warning": False,
            "detail": "Metadata passed",
        }
    else:
        from app.internal.cms_biz_orchestration.services.metadata_validation_service import check_metadata_complete
        meta_complete, missing_fields = await check_metadata_complete(db, content_id, ct)
        result["Metadata"] = {
            "completed": meta_complete,
            "warning": False,
            "detail": (
                f"Missing required fields: {', '.join(missing_fields)}"
                if missing_fields else "All required metadata fields present"
            ),
        }

    # ── Posters: 复用 ContentStatusService 的统一判断 ──
    result["Posters"] = await ContentStatusService.get_posters_completion(
        db, content_id, ct
    )

    # ── CastRoleMap: 检查是否有演员角色映射 ──
    if ct in ("MOVIE", "EPISODE", "SERIES", "SEASON_SERIES", "SEASON", "SCHEDULE"):
        crm_count = (await db.execute(
            select(func.count()).select_from(CastRoleMap).where(
                CastRoleMap.content_id == content_id,
                CastRoleMap.is_deleted.is_(False),
            )
        )).scalar() or 0
        result["CastRoleMap"] = {
            "completed": crm_count > 0,
            "warning": False,
            "detail": f"{crm_count} cast role(s)" if crm_count else "No cast roles",
        }

    # ── Category: 检查是否绑定了栏目 ──
    cat_count = (await db.execute(
        select(func.count()).select_from(ContentCategory).where(
            ContentCategory.content_id == content_id,
            ContentCategory.is_deleted.is_(False),
            ContentCategory.is_discarded.is_(False),
        )
    )).scalar() or 0
    result["Category"] = {
        "completed": cat_count > 0,
        "warning": False,
        "detail": f"{cat_count} categor(ies)" if cat_count else "No categories",
    }

    # ── Package: 检查是否绑定了服务包 ──
    if ct in ("MOVIE", "EPISODE", "SERIES", "SEASON_SERIES", "SEASON", "CHANNEL"):
        pkg_count = (await db.execute(
            select(func.count()).select_from(ContentPackage).where(
                ContentPackage.content_id == content_id,
                ContentPackage.is_deleted.is_(False),
                ContentPackage.is_discarded.is_(False),
            )
        )).scalar() or 0
        result["Package"] = {
            "completed": pkg_count > 0,
            "warning": False,
            "detail": f"{pkg_count} package(s)" if pkg_count else "No packages",
        }

    # ── Trailer: 检查是否有预告片类型的 Movie ──
    if ct in ("MOVIE", "EPISODE", "SERIES", "SEASON_SERIES", "SEASON"):
        trailer_count = (await db.execute(
            select(func.count()).select_from(Movie).where(
                Movie.content_id == content_id,
                Movie.is_deleted.is_(False),
                Movie.movie_type == 2,
            )
        )).scalar() or 0
        result["Trailer"] = {
            "completed": trailer_count > 0,
            "warning": trailer_count == 0,
            "detail": f"{trailer_count} trailer(s)" if trailer_count else "No trailers (optional)",
        }

    # ── MusicEffects: 检查是否有字幕类型的 Movie ──
    if ct in ("MOVIE", "EPISODE", "SERIES", "SEASON_SERIES", "SEASON"):
        me_count = (await db.execute(
            select(func.count()).select_from(Movie).where(
                Movie.content_id == content_id,
                Movie.is_deleted.is_(False),
                Movie.movie_type == 3,
            )
        )).scalar() or 0
        result["MusicEffects"] = {
            "completed": me_count > 0,
            "warning": me_count == 0,
            "detail": f"{me_count} file(s)" if me_count else "No music/effects (optional)",
        }

    # ── InjectSubContent: 检查是否有子内容 ──
    if ct in ("SERIES", "SEASON_SERIES"):
        child_count = (await db.execute(
            select(func.count()).select_from(Content).where(
                Content.parent_id == content_id,
                Content.content_type == "EPISODE",
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )).scalar() or 0
        result["InjectSubContent"] = {
            "completed": child_count > 0,
            "warning": False,
            "detail": f"{child_count} episode(s)" if child_count else "No episodes",
        }
    elif ct == "SEASON":
        child_count = (await db.execute(
            select(func.count()).select_from(Content).where(
                Content.parent_id == content_id,
                Content.content_type == "SEASON_SERIES",
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )).scalar() or 0
        result["InjectSubContent"] = {
            "completed": child_count > 0,
            "warning": False,
            "detail": f"{child_count} season series" if child_count else "No season series",
        }

    # ── PhysicalChannel: 检查是否有物理频道 ──
    if ct == "CHANNEL":
        pc_count = (await db.execute(
            select(func.count()).select_from(PhysicalChannel).where(
                PhysicalChannel.channel_id == content_id,
                PhysicalChannel.is_deleted.is_(False),
                PhysicalChannel.is_discarded.is_(False),
            )
        )).scalar() or 0
        result["InjectSubContent"] = {
            "completed": pc_count > 0,
            "warning": False,
            "detail": f"{pc_count} physical channel(s)" if pc_count else "No physical channels",
        }
        result["PhysicalChannel"] = result["InjectSubContent"]

    # ── ApplicationReview: 发起审核即打勾，取ContentProcess最新记录判断 ──
    latest_app_review = (await db.execute(
        select(ContentProcess).where(
            ContentProcess.content_id == content_id,
            ContentProcess.node_code == "ApplicationReview",
            ContentProcess.is_deleted.is_(False),
        ).order_by(ContentProcess.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    app_review_passed = latest_app_review is not None and latest_app_review.status == "Passed"
    result["ApplicationReview"] = {
        "completed": app_review_passed,
        "warning": False,
        "detail": "Applied" if app_review_passed else "Not applied",
    }

    # ── ContentReview / PublishPlan: 以内容状态优先级判断 ──
    # None=0, WaitingForMaterials=1, InProgress=2, ReadyForPublish=3,
    # Publishing=4, PublishFailed=5, Published=6, NoActiveLicense=7, Closed=8
    STATUS_PRIORITY = {
        "None": 0, "WaitingForMaterials": 1, "InProgress": 2,
        "ReadyForPublish": 3, "Publishing": 4, "PublishFailed": 5,
        "Published": 6, "NoActiveLicense": 7, "Closed": 8,
    }
    current_priority = STATUS_PRIORITY.get(content.status, 0)
    content_review_completed = current_priority >= 3
    publish_completed = current_priority >= 4

    result["ContentReview"] = {
        "completed": content_review_completed,
        "warning": False,
        "detail": "Approved" if content_review_completed else "Not approved",
    }
    result["PublishPlan"] = {
        "completed": publish_completed,
        "warning": False,
        "detail": "Published" if publish_completed else "Not published",
    }

    # ── 已发布/已下架/许可证过期状态：必填节点强制完成（补救措施） ──
    # 场景：总季编辑元数据同步给子集后，子集 Metadata 被置 Pending，
    # 级联发布后子集状态变为 Published，但 Metadata 节点仍显示红色 X。
    # 此处统一兜底，Published/Closed/NoActiveLicense 状态下必填节点强制打勾。
    # NoActiveLicense（优先级 7 > Published=6）是发布后的下游状态（许可证过期任务
    # 只改 content.status、不动流程记录），提交审核等节点不应因过期回退为红叉。
    # 可选节点（workflow_node_config.mandatory=False，如 Trailer/MusicEffects）
    # 按实际数据状态显示：未上传不打勾、上传才打勾。
    if content.status in ("Published", "Closed", "NoActiveLicense"):
        optional_node_codes = await _get_optional_node_codes(db, content)
        for key in result:
            if key in optional_node_codes:
                continue
            result[key]["completed"] = True
            if result[key].get("warning"):
                result[key]["warning"] = False

    return result


# ─── VOD Excel 导出/导入 ───────────────────────────────────────────────

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


# 注意：导出（47 列）与导入模板（32 列）不再一致：导出仅供查看/对账，不可直接再导入。
# Series Type 列已从导入模板与导出中移除（统一以 Content Type 区分内容类型，
# series_type 由 Content Type 推导：SERIES=1/SEASON_SERIES=2/SEASON=3）。

# VOD 导入模板表头（32 列）—— 严格模式：
# 必填字段表头带红色 (*) 标记（依据 VOD 详情页元数据编辑表单必填项）：
# Content Name / Genre / Type / VodType / RatingLevel / Metalayout；
# OriginalName 仅系列类型（SERIES/SEASON_SERIES/SEASON）必填，导入时条件校验。
# 导入时按表头名称匹配，(*) 后缀会被忽略。
VOD_IMPORT_HEADERS = [
    # Content 主表（6）：Content Type 决定内容类型；Series Type 已移除（由 Content Type 推导）
    "Content ID", "Content Type", "Content Name(*)", "Parent Name",
    "Series Ordinal", "Sequence",
    # 关联（6）：Genre/Type/Tags/Custom Tags/Category/Package
    "Genre(*)", "Type(*)", "Tags", "Custom Tags", "Category", "Package",
    # 元数据-字典（4）
    "VodType(*)", "Language", "RatingLevel(*)", "Advice",
    # 元数据-名称（5）
    "SortName", "OriginalName", "OriginalCountry", "ShortTitle", "ReleaseYear",
    # 元数据-描述（2）
    "Description", "Studio",
    # 元数据-评分（1）：RatingType/RatingId 已移除（评分来源信息不通过导入维护）
    "Rating",
    # 元数据-音视频（4）
    "AudioLang", "SubtitleLang", "BeginDuration", "EndDuration",
    # 元数据-标识（3）：CdrId/SeriesFlag 已移除
    "Keywords", "Metalayout(*)", "StatusFlag",
    # 元数据-章节（1）
    "SectionsInfo",
]

# VOD 导入必填字段（无条件必填；OriginalName 为系列类型条件必填，不在本列表）
VOD_IMPORT_REQUIRED_HEADERS = [
    "Content Name", "Genre", "Type", "VodType", "RatingLevel", "Metalayout",
]


def _normalize_vod_header(h) -> str:
    """规范化导入表头：去首尾空白并去掉必填标记后缀 (*)。"""
    if h is None:
        return ""
    return str(h).strip().replace("(*)", "").strip()


async def export_vod_contents_excel(db: AsyncSession, ids: list[int]) -> bytes:
    """导出 VOD 内容为 Excel 文件（47 列）。

    比导入模板（34 列）多出：Ingest Status / Is Discarded / Channel Name /
    Begin Time / End Time / Provider / License Start / License End /
    Unpublish Date / Publish Date / SeriesFlag / Created At / Updated At，
    仅供查看/对账，导出文件不可直接再导入（Series Type 已移除，
    统一以 Content Type 区分内容类型）。
    """
    import openpyxl
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    rows = (
        await db.execute(
            select(Content).where(
                Content.id.in_(ids),
                Content.is_deleted.is_(False),
            )
        )
    ).scalars().all()

    # 批量查询父级内容标题，避免 N+1 查询
    parent_ids = {r.parent_id for r in rows if r.parent_id is not None}
    parent_name_map: dict[int, str] = {}
    if parent_ids:
        parent_rows = (
            await db.execute(
                select(Content.id, Content.title).where(Content.id.in_(parent_ids))
            )
        ).all()
        parent_name_map = {pid: title for pid, title in parent_rows}

    # 查询 4 个字典字段，构建 code→name 映射
    vod_type_dict = await get_dict_children_by_code(db, "VodType")
    vod_type_map: dict[str, str] = {item.code: item.name for item in vod_type_dict}
    language_dict = await get_dict_children_by_code(db, "Language")
    language_map: dict[str, str] = {item.code: item.name for item in language_dict}
    rating_level_dict = await get_dict_children_by_code(db, "RatingLevel")
    rating_level_map: dict[str, str] = {item.code: item.name for item in rating_level_dict}
    advice_dict = await get_dict_children_by_code(db, "Advice")
    advice_map: dict[str, str] = {item.code: item.name for item in advice_dict}

    # 批量查询元数据（program_metadata + series_metadata），避免 N+1
    content_ids = [r.id for r in rows]
    movie_type_ids = {ContentType.MOVIE.value, ContentType.EPISODE.value}
    series_type_ids = {ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.SEASON.value}

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

        series_metas = (
            await db.execute(
                select(SeriesMetadata).where(
                    SeriesMetadata.content_id.in_(content_ids),
                    SeriesMetadata.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        series_meta_map = {m.content_id: m for m in series_metas}

    # Tags（元数据中的 tag_ids，从 ContentMetadata/SeriesMetadata 表获取 tag_id，再通过 Tag 表获取名称）
    all_tag_ids: set[int] = set()
    for meta in list(program_meta_map.values()) + list(series_meta_map.values()):
        if meta.tag_ids:
            all_tag_ids.update(meta.tag_ids)
    tag_name_map: dict[int, str] = {}
    if all_tag_ids:
        tag_rows = (await db.execute(select(Tag.id, Tag.name).where(Tag.id.in_(list(all_tag_ids))))).all()
        tag_name_map = {tid: tname for tid, tname in tag_rows}
    # 构建 content_id -> tag_names 映射
    tag_names_map: dict[int, list[str]] = {cid: [] for cid in content_ids}
    for meta in list(program_meta_map.values()) + list(series_meta_map.values()):
        if meta.tag_ids and meta.content_id in tag_names_map:
            tag_names_map[meta.content_id] = [tag_name_map.get(tid, "") for tid in meta.tag_ids if tag_name_map.get(tid)]

    # 批量查询来源节目单与频道：content.source_schedule_id → SCHEDULE → CHANNEL
    # 用于 Channel Name / Begin Time / End Time 三列（归档产物有值，手工内容为空）
    src_schedule_ids = {r.source_schedule_id for r in rows if r.source_schedule_id is not None}
    src_schedule_map: dict[int, Content] = {}
    src_channel_parent_ids: set[int] = set()
    if src_schedule_ids:
        src_schedule_rows = (
            await db.execute(
                select(Content).where(
                    Content.id.in_(src_schedule_ids),
                    Content.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        src_schedule_map = {s.id: s for s in src_schedule_rows}
        src_channel_parent_ids = {s.parent_id for s in src_schedule_rows if s.parent_id is not None}
    src_channel_name_map: dict[int, str] = {}
    if src_channel_parent_ids:
        src_channel_rows = (
            await db.execute(
                select(Content.id, Content.title).where(
                    Content.id.in_(src_channel_parent_ids),
                    Content.is_deleted.is_(False),
                )
            )
        ).all()
        src_channel_name_map = {cid: title for cid, title in src_channel_rows}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "VOD Contents"

    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    # 富文本字体：字段名白色 + (*) 红色（必填标记，仅供参考，导出文件不可直接导入）
    base_inline = InlineFont(rFont="Calibri", b=True, color="FFFFFF")
    mark_inline = InlineFont(rFont="Calibri", b=True, color="FF0000")

    # 47 列导出表头（比 34 列导入模板多 13 列；Series Type 已从导出移除，统一以 Content Type 区分）
    headers = [
        "Content ID", "Content Type", "Content Name(*)", "Parent Name",
        "Series Ordinal", "Sequence",
        "Ingest Status", "Is Discarded", "Channel Name", "Begin Time", "End Time",
        "Genre(*)", "Type(*)", "Tags", "Custom Tags", "Category", "Package",
        "Provider", "License Start", "License End", "Unpublish Date", "Publish Date",
        "VodType(*)", "Language", "RatingLevel(*)", "Advice",
        "SortName", "OriginalName", "OriginalCountry", "ShortTitle", "ReleaseYear",
        "Description", "Studio", "Rating", "RatingType", "RatingId",
        "AudioLang", "SubtitleLang", "BeginDuration", "EndDuration",
        "Keywords", "Metalayout(*)", "StatusFlag", "SeriesFlag", "SectionsInfo",
        "Created At", "Updated At",
    ]
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

    # 列宽（47 列）
    col_widths = [
        12, 14, 30, 24, 14, 12,                   # Content 主表（6）
        14, 12, 20, 20, 20,                        # Ingest Status/Is Discarded/来源节目单（5）
        20, 16, 20, 20, 20, 20,                    # 关联（6）
        20, 20, 20, 20, 20,                        # Provider/License/Publish（5）
        20, 16, 16, 20,                            # 元数据-字典
        20, 20, 16, 16, 12,                        # 元数据-名称
        40, 20,                                    # 元数据-描述
        12, 16, 16,                                # 元数据-评分
        20, 20, 12, 12,                            # 元数据-音视频
        20, 16, 12, 12, 12,                        # 元数据-标识（5）
        40, 20, 20,                                # 章节 + 审计时间
    ]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    # 预加载 Metalayout 字典，构建 code→name 映射
    metalayout_dict = await get_dict_children_by_code(db, "Metalayout")
    metalayout_map: dict[str, str] = {item.code: item.name for item in metalayout_dict}

    def _fmt_dt(val) -> str:
        """datetime → 本地时区字符串（与节目单导出格式一致）。"""
        return val.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if val else ""

    for row_idx, r in enumerate(rows, 2):
        item = await _build_vod_item(db, r)

        # Content 主表（1-6）；Series Type 已移除（统一以 Content Type 区分）
        ws.cell(row=row_idx, column=1, value=r.id)
        ws.cell(row=row_idx, column=2, value=r.content_type)
        ws.cell(row=row_idx, column=3, value=r.title)
        ws.cell(row=row_idx, column=4, value=parent_name_map.get(r.parent_id, "") if r.parent_id else "")
        ws.cell(row=row_idx, column=5, value=r.series_ordinal if r.series_ordinal is not None else "")
        ws.cell(row=row_idx, column=6, value=r.sequence if r.sequence is not None else "")

        # 主表扩展（7-11）：Ingest Status / Is Discarded / 来源节目单信息
        ws.cell(row=row_idx, column=7, value=r.status or "")
        ws.cell(row=row_idx, column=8, value="YES" if r.is_discarded else "NO")
        src_schedule = src_schedule_map.get(r.source_schedule_id) if r.source_schedule_id else None
        if src_schedule is not None:
            ws.cell(row=row_idx, column=9, value=src_channel_name_map.get(src_schedule.parent_id, "") if src_schedule.parent_id else "")
            ws.cell(row=row_idx, column=10, value=_fmt_dt(src_schedule.begin_time))
            ws.cell(row=row_idx, column=11, value=_fmt_dt(src_schedule.end_time))

        # 关联（12-17）：Genre/Type/Tags/Custom Tags/Category/Package
        ws.cell(row=row_idx, column=12, value=item.genre_name or "")
        ws.cell(row=row_idx, column=13, value=item.type_name or "")
        # Tags（元数据中的 tag_ids）
        tag_names_for_content = tag_names_map.get(r.id, [])
        ws.cell(row=row_idx, column=14, value=", ".join(tag_names_for_content) if tag_names_for_content else "")
        # Custom Tags（中间表）
        ws.cell(row=row_idx, column=15, value=", ".join(item.custom_tag_names) if item.custom_tag_names else "")
        ws.cell(row=row_idx, column=16, value=item.category_name or "")
        ws.cell(row=row_idx, column=17, value=", ".join(item.package_names) if item.package_names else "")

        # 版权/发布（18-22）：Provider / License / Publish
        ws.cell(row=row_idx, column=18, value=", ".join(item.provider_names) if item.provider_names else "")
        ws.cell(row=row_idx, column=19, value=item.license_start or "")
        ws.cell(row=row_idx, column=20, value=item.license_end or "")
        ws.cell(row=row_idx, column=21, value=item.unpublish_date or "")
        ws.cell(row=row_idx, column=22, value=item.publish_date or "")

        # 元数据：从 program_metadata / series_metadata 获取
        meta = program_meta_map.get(r.id) if r.content_type in movie_type_ids else series_meta_map.get(r.id)
        vod_type_codes = getattr(meta, "vod_type", None) if meta else None
        language_code = getattr(meta, "language", None) if meta else None
        rating_level_code = getattr(meta, "rating_level", None) if meta else None
        advice_codes = getattr(meta, "advice", None) if meta else None
        audio_lang_codes = getattr(meta, "audio_lang", None) if meta else None
        subtitle_lang_codes = getattr(meta, "subtitle_lang", None) if meta else None
        keywords_arr = getattr(meta, "keywords", None) if meta else None
        metalayout_code = getattr(meta, "metalayout", None) if meta else None

        # 元数据-字典（23-26）
        ws.cell(row=row_idx, column=23, value=", ".join(vod_type_map.get(c, c) for c in vod_type_codes) if vod_type_codes else "")
        ws.cell(row=row_idx, column=24, value=language_map.get(language_code, language_code) if language_code else "")
        ws.cell(row=row_idx, column=25, value=rating_level_map.get(rating_level_code, rating_level_code) if rating_level_code else "")
        ws.cell(row=row_idx, column=26, value=", ".join(advice_map.get(c, c) for c in advice_codes) if advice_codes else "")

        # 元数据-名称（27-31）—— 文本字段强制 str，避免 openpyxl 将数字串转为 int
        ws.cell(row=row_idx, column=27, value=str(getattr(meta, "sort_name", None) or ""))
        ws.cell(row=row_idx, column=28, value=str(getattr(meta, "original_name", None) or ""))
        ws.cell(row=row_idx, column=29, value=str(getattr(meta, "original_country", None) or ""))
        ws.cell(row=row_idx, column=30, value=str(getattr(meta, "short_title", None) or ""))
        ws.cell(row=row_idx, column=31, value=getattr(meta, "release_year", None) if meta and meta.release_year is not None else "")

        # 元数据-描述（32-33）
        ws.cell(row=row_idx, column=32, value=str(getattr(meta, "description", None) or ""))
        ws.cell(row=row_idx, column=33, value=str(getattr(meta, "studio", None) or ""))

        # 元数据-评分（34-36）
        ws.cell(row=row_idx, column=34, value=str(getattr(meta, "rating", None) or ""))
        ws.cell(row=row_idx, column=35, value=str(getattr(meta, "rating_type", None) or ""))
        ws.cell(row=row_idx, column=36, value=str(getattr(meta, "rating_id", None) or ""))

        # 元数据-音视频（37-40）
        ws.cell(row=row_idx, column=37, value=", ".join(language_map.get(c, c) for c in audio_lang_codes) if audio_lang_codes else "")
        ws.cell(row=row_idx, column=38, value=", ".join(language_map.get(c, c) for c in subtitle_lang_codes) if subtitle_lang_codes else "")
        ws.cell(row=row_idx, column=39, value=getattr(meta, "begin_duration", None) if meta and meta.begin_duration is not None else "")
        ws.cell(row=row_idx, column=40, value=getattr(meta, "end_duration", None) if meta and meta.end_duration is not None else "")

        # 元数据-标识（41-44）
        ws.cell(row=row_idx, column=41, value=_format_keywords_export(keywords_arr))
        ws.cell(row=row_idx, column=42, value=metalayout_map.get(metalayout_code, metalayout_code) if metalayout_code else "")
        # StatusFlag：meta 不存在时留空（None），存在时输出 YES/NO
        if meta:
            ws.cell(row=row_idx, column=43, value="YES" if getattr(meta, "status_flag", False) else "NO")
        # SeriesFlag：连续剧标识（0=VOD/MOVIE, 1=Series/EPISODE），meta 不存在时留空
        if meta is not None:
            ws.cell(row=row_idx, column=44, value=getattr(meta, "series_flag", None))

        # 元数据-章节（45）
        sections_info = getattr(meta, "sections_info", None) if meta else None
        ws.cell(row=row_idx, column=45, value=json.dumps(sections_info, ensure_ascii=False) if sections_info else "")

        # 审计时间（46-47）
        ws.cell(row=row_idx, column=46, value=_fmt_dt(r.created_at))
        ws.cell(row=row_idx, column=47, value=_fmt_dt(r.updated_at))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()


class VodImportError(BaseModel):
    row: int
    errors: list[str] = []


class VodImportResult(BaseModel):
    total: int
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[VodImportError] = []


async def import_vod_contents_excel(
    db: AsyncSession,
    file: UploadFile,
    processed_by: str | None = None,
) -> VodImportResult:
    """从 Excel 导入 VOD 内容（仅元数据，严格模式）。

    - Content ID 为空 → 新建；存在 → 更新（以最新数据覆盖）
    - 严格表头校验：必须为 32 列导入模板表头（必填列带 (*) 标记，匹配时忽略），
      缺失或多余列（如旧版文件的 External ID/Provider/CdrId/RatingType/RatingId 等）→ 整个文件拒绝
    - 严格必填校验：任一行必填字段缺失（Content Name/Genre/Type/VodType/
      RatingLevel/Metalayout，系列类型另需 OriginalName）→ 整个文件拒绝，不写入任何数据
    - Provider/License 列已移除：导入不再创建 Contract/License（只导入元数据）
    """
    import openpyxl

    contents = await file.read()
    wb = openpyxl.load_workbook(io.BytesIO(contents))
    ws = wb.active

    result = VodImportResult(total=max(0, ws.max_row - 1))

    # ── 表头校验（严格模式）───────────────────────────────
    # 严格按 32 列导入模板执行：缺失或多余列均直接拒绝，不兼容旧版文件
    actual_headers = [cell.value for cell in ws[1]]
    normalized_headers = [_normalize_vod_header(h) for h in actual_headers]
    expected_headers = [_normalize_vod_header(h) for h in VOD_IMPORT_HEADERS]
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

    header_indices: dict[str, int] = {h: idx for idx, h in enumerate(normalized_headers) if h}

    # ── 必填字段前置校验（严格模式）───────────────────────
    # 任一行必填字段缺失 → 整个文件拒绝导入，不写入任何数据。
    # OriginalName 为系列类型（SERIES/SEASON_SERIES/SEASON）条件必填，
    # 行内容类型 = Content Type 列值；为空时若 Content ID 已存在则取库中类型，否则缺省 MOVIE。
    def _row_cell(row, key: str):
        idx = header_indices.get(key)
        return row[idx].value if idx is not None else None

    def _is_blank_row(row) -> bool:
        return all(v is None or str(v).strip() == "" for v in (c.value for c in row))

    rows = [ws[r] for r in range(2, ws.max_row + 1)]

    # 预查询已存在 Content ID 的类型，用于系列类型条件必填判断
    existing_ids: list[int] = []
    for row in rows:
        if _is_blank_row(row):
            continue
        v = _row_cell(row, "Content ID")
        if v is None or str(v).strip() == "":
            continue
        try:
            existing_ids.append(int(str(v).strip()))
        except (ValueError, TypeError):
            continue
    existing_type_map: dict[int, str] = {}
    if existing_ids:
        type_rows = (
            await db.execute(
                select(Content.id, Content.content_type).where(Content.id.in_(existing_ids))
            )
        ).all()
        existing_type_map = {cid: ctype for cid, ctype in type_rows}

    required_errors: list[str] = []
    for idx, row in enumerate(rows, start=2):
        if _is_blank_row(row):
            continue
        missing_fields: list[str] = []
        for h in VOD_IMPORT_REQUIRED_HEADERS:
            v = _row_cell(row, h)
            if v is None or str(v).strip() == "":
                missing_fields.append(h)
        # 系列类型条件必填：OriginalName
        ct_val = _row_cell(row, "Content Type")
        if ct_val is not None and str(ct_val).strip():
            effective_type = str(ct_val).strip()
        else:
            cid_val = _row_cell(row, "Content ID")
            try:
                cid_int = int(str(cid_val).strip()) if cid_val is not None and str(cid_val).strip() else None
            except (ValueError, TypeError):
                cid_int = None
            effective_type = existing_type_map.get(cid_int, ContentType.MOVIE.value)
        if effective_type in (
            ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.SEASON.value,
        ):
            ov = _row_cell(row, "OriginalName")
            if ov is None or str(ov).strip() == "":
                missing_fields.append("OriginalName")
        if missing_fields:
            required_errors.append(f"Row {idx}: {', '.join(missing_fields)}")

    if required_errors:
        shown = required_errors[:20]
        suffix = f" ...（共 {len(required_errors)} 行缺失必填字段）" if len(required_errors) > 20 else ""
        raise BusinessException(
            ErrorCode.EXCEL_IMPORT_FAILED,
            get_msg("EXCEL_IMPORT_FAILED") + ": " + "; ".join(shown) + suffix,
        )

    # ── 预加载缓存 ──────────────────────────────────────────────
    # 字典字段：name → code（用于匹配 Excel 中的文字）
    dict_specs = [
        ("VodType", "vod_type"),
        ("Language", "language"),
        ("RatingLevel", "rating_level"),
        ("Advice", "advice"),
    ]
    dict_caches: dict[str, dict[str, str]] = {}
    dict_root_ids: dict[str, int] = {}
    for dict_code, _ in dict_specs:
        children = await get_dict_children_by_code(db, dict_code)
        dict_caches[dict_code] = {item.name: item.code for item in children}
        root = (
            await db.execute(
                select(DictNode).where(
                    DictNode.parent_id.is_(None),
                    DictNode.code == dict_code,
                    DictNode.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if root:
            dict_root_ids[dict_code] = root.id

    # 基础数据：name → id（Genre / ContentType / CustomTag 支持自动创建）
    genre_cache: dict[str, int] = {g.name: g.id for g in (await db.execute(select(Genre).where(Genre.is_deleted.is_(False)))).scalars().all()}
    type_cache: dict[str, int] = {t.name: t.id for t in (await db.execute(select(ContentTypeModel).where(ContentTypeModel.is_deleted.is_(False)))).scalars().all()}
    tag_cache: dict[str, int] = {t.name: t.id for t in (await db.execute(select(CustomTag).where(CustomTag.is_deleted.is_(False)))).scalars().all()}

    # Tag 表（用于 Tags 字段，元数据中的 tag_ids，严格匹配，不创建）
    sys_tag_cache: dict[str, int] = {t.name: t.id for t in (await db.execute(select(Tag).where(Tag.is_deleted.is_(False)))).scalars().all()}

    # 关联数据：name → id（Category / Package 仅匹配，不自动创建；Provider 已从导入模板移除）
    cat_cache: dict[str, int] = {c.name: c.id for c in (await db.execute(select(Category).where(Category.is_deleted.is_(False)))).scalars().all()}
    pkg_cache: dict[str, int] = {p.name: p.id for p in (await db.execute(select(Package).where(Package.is_deleted.is_(False)))).scalars().all()}

    movie_type_ids = {ContentType.MOVIE.value, ContentType.EPISODE.value}
    series_type_ids = {ContentType.SERIES.value, ContentType.SEASON_SERIES.value, ContentType.SEASON.value}

    # ── 逐行处理 ──────────────────────────────────────────────
    for row_idx in range(2, ws.max_row + 1):
        row = ws[row_idx]

        # 记录处理前缓存快照，行回滚时撤销新增的缓存项
        cache_snapshots = {
            "genre": dict(genre_cache), "type": dict(type_cache), "tag": dict(tag_cache),
        }
        for dc in dict_caches.values():
            cache_snapshots[id(dc)] = dict(dc)

        try:
            async with db.begin_nested():
                created, is_new = await _process_vod_row(
                    db, row, header_indices,
                    dict_caches, dict_root_ids,
                    genre_cache, type_cache, tag_cache, sys_tag_cache,
                    cat_cache, pkg_cache,
                    movie_type_ids, series_type_ids,
                    processed_by=processed_by,
                )
        except _RowSkipError as e:
            _restore_caches(genre_cache, type_cache, tag_cache, dict_caches, cache_snapshots)
            result.errors.append(VodImportError(row=row_idx, errors=e.errors))
            result.skipped += 1
            continue
        except Exception as e:
            _restore_caches(genre_cache, type_cache, tag_cache, dict_caches, cache_snapshots)
            logger.exception(f"VOD 导入第 {row_idx} 行异常: {e}")
            result.errors.append(VodImportError(row=row_idx, errors=[f"处理异常: {e}"]))
            result.skipped += 1
            continue

        if is_new:
            result.created += 1
        elif created:
            result.updated += 1

    await db.commit()
    return result


def _restore_caches(
    genre_cache: dict, type_cache: dict, tag_cache: dict,
    dict_caches: dict, snapshots: dict,
) -> None:
    """行回滚后恢复缓存到处理前状态，避免引用已回滚的 ID。"""
    genre_cache.clear()
    genre_cache.update(snapshots["genre"])
    type_cache.clear()
    type_cache.update(snapshots["type"])
    tag_cache.clear()
    tag_cache.update(snapshots["tag"])
    for dc in dict_caches.values():
        snap = snapshots.get(id(dc))
        if snap is not None:
            dc.clear()
            dc.update(snap)


class _RowSkipError(Exception):
    """行级跳过异常，携带错误信息列表。"""
    def __init__(self, errors: list[str]):
        self.errors = errors


def _cell_str(value) -> str:
    """将单元格值转换为字符串，空值返回空字符串。"""
    if value is None:
        return ""
    return str(value).strip()


def _apply_meta_fields(
    meta,
    vod_type_codes,
    language_code,
    rating_level_code,
    advice_codes,
    audio_lang_codes,
    subtitle_lang_codes,
    metalayout_code,
    sort_name,
    original_name,
    original_country,
    short_title,
    release_year,
    description,
    studio,
    rating,
    begin_duration,
    end_duration,
    keywords,
    cdr_id,
    status_flag,
    sections_info,
) -> None:
    """将解析后的元数据字段统一写入 ContentMetadata / SeriesMetadata 实例。"""
    if vod_type_codes:
        meta.vod_type = vod_type_codes
    if language_code:
        meta.language = language_code
    if rating_level_code:
        meta.rating_level = rating_level_code
    if advice_codes:
        meta.advice = advice_codes
    if audio_lang_codes:
        meta.audio_lang = audio_lang_codes
    if subtitle_lang_codes:
        meta.subtitle_lang = subtitle_lang_codes
    if metalayout_code:
        meta.metalayout = metalayout_code
    if sort_name:
        meta.sort_name = sort_name
    if original_name:
        meta.original_name = original_name
    if original_country:
        meta.original_country = original_country
    if short_title:
        meta.short_title = short_title
    if release_year is not None:
        meta.release_year = release_year
    if description:
        meta.description = description
    if studio:
        meta.studio = studio
    if rating:
        meta.rating = rating
    if begin_duration is not None:
        meta.begin_duration = begin_duration
    if end_duration is not None:
        meta.end_duration = end_duration
    if keywords:
        meta.keywords = keywords
    if cdr_id:
        meta.cdr_id = cdr_id
    if status_flag is not None:
        meta.status_flag = status_flag
    if sections_info is not None:
        meta.sections_info = sections_info


async def _resolve_dict_codes(
    db: AsyncSession,
    dict_code: str,
    text_value,
    dict_caches: dict[str, dict[str, str]],
    dict_root_ids: dict[str, int],
) -> list[str]:
    """解析字典字段（多值）：按 name 匹配 code，未匹配则自动创建 DictNode。"""
    if not text_value:
        return []
    names = [n.strip() for n in str(text_value).split(",") if n.strip()]
    cache = dict_caches.setdefault(dict_code, {})
    codes: list[str] = []
    for name in names:
        code = cache.get(name)
        if code:
            codes.append(code)
            continue
        root_id = dict_root_ids.get(dict_code)
        if root_id is None:
            continue
        new_code = uuid.uuid4().hex[:8].upper()
        node = DictNode(
            parent_id=root_id, code=new_code, name=name,
            sort_order=0, status="active", is_system=False,
        )
        db.add(node)
        await db.flush()
        cache[name] = new_code
        codes.append(new_code)
    return codes


async def _resolve_single_dict_code(
    db: AsyncSession,
    dict_code: str,
    text_value,
    dict_caches: dict[str, dict[str, str]],
    dict_root_ids: dict[str, int],
) -> Optional[str]:
    """解析字典字段（单值）：按 name 匹配 code，未匹配则自动创建 DictNode。"""
    if not text_value:
        return None
    name = str(text_value).strip()
    if not name:
        return None
    cache = dict_caches.setdefault(dict_code, {})
    code = cache.get(name)
    if code:
        return code
    root_id = dict_root_ids.get(dict_code)
    if root_id is None:
        return None
    new_code = uuid.uuid4().hex[:8].upper()
    node = DictNode(
        parent_id=root_id, code=new_code, name=name,
        sort_order=0, status="active", is_system=False,
    )
    db.add(node)
    await db.flush()
    cache[name] = new_code
    return new_code


async def _resolve_basic_data(
    db: AsyncSession,
    text_value,
    cache: dict[str, int],
    model_cls,
    default_lang: str = "zh",
) -> list[int]:
    """解析基础数据（多值）：按 name 匹配 id，未匹配则自动创建。"""
    if not text_value:
        return []
    names = [n.strip() for n in str(text_value).split(",") if n.strip()]
    ids: list[int] = []
    for name in names:
        existing_id = cache.get(name)
        if existing_id:
            ids.append(existing_id)
            continue
        obj = model_cls(name=name) if model_cls == ContentTypeModel else model_cls(name=name, language=default_lang)
        db.add(obj)
        await db.flush()
        cache[name] = obj.id
        ids.append(obj.id)
    return ids


def _resolve_basic_data_strict(
    text_value,
    cache: dict[str, int],
    field_label: str,
    errors: list[str],
) -> list[int]:
    """解析基础数据（多值，严格模式）：仅按 name 匹配 id，未匹配不创建，记录错误。"""
    if not text_value:
        return []
    names = [n.strip() for n in str(text_value).split(",") if n.strip()]
    if not names:
        return []
    ids: list[int] = []
    all_matched = True
    for name in names:
        existing_id = cache.get(name)
        if existing_id:
            ids.append(existing_id)
        else:
            errors.append(f"{field_label} not found: {name}")
            all_matched = False
    return ids if all_matched else []





def _resolve_relation_ids(
    text_value,
    cache: dict[str, int],
    field_label: str,
    errors: list[str],
) -> list[int]:
    """解析关联数据（多值，严格模式）：仅匹配，不自动创建，未匹配记录错误并返回空列表。"""
    if not text_value:
        return []
    names = [n.strip() for n in str(text_value).split(",") if n.strip()]
    if not names:
        return []
    ids: list[int] = []
    all_matched = True
    for name in names:
        existing_id = cache.get(name)
        if existing_id:
            ids.append(existing_id)
        else:
            errors.append(f"{field_label} not found: {name}")
            all_matched = False
    return ids if all_matched else []


def _parse_date(val) -> Optional[date]:
    """将 Excel 日期值解析为 date 对象。"""
    if not val:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    try:
        return datetime.strptime(str(val).strip(), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


async def _process_vod_row(
    db: AsyncSession,
    row,
    header_indices: dict[str, int],
    dict_caches: dict[str, dict[str, str]],
    dict_root_ids: dict[str, int],
    genre_cache: dict[str, int],
    type_cache: dict[str, int],
    tag_cache: dict[str, int],
    sys_tag_cache: dict[str, int],
    cat_cache: dict[str, int],
    pkg_cache: dict[str, int],
    movie_type_ids: set,
    series_type_ids: set,
    processed_by: str | None = None,
) -> tuple[bool, bool]:
    """处理单行数据，返回 (是否处理成功, 是否新建)。失败时抛出 _RowSkipError。

    注意：必填字段（Content Name/Genre/Type/VodType/RatingLevel/Metalayout 及
    系列类型的 OriginalName）已在 import_vod_contents_excel 前置校验中保证非空；
    External ID/Ingest Status/Is Discarded/Provider/License/CdrId/SeriesFlag
    列已从导入模板移除，此处不再解析。
    """
    def cell(key: str):
        return row[header_indices[key]].value

    def cell_optional(key: str):
        """读取可选列，表头不存在时返回 None"""
        idx = header_indices.get(key)
        return row[idx].value if idx is not None else None

    errors: list[str] = []

    # ── Content ID 解析 ──
    content_id_val = cell("Content ID")
    content_id: Optional[int] = None
    if content_id_val:
        try:
            content_id = int(content_id_val)
        except (ValueError, TypeError):
            errors.append(f"Invalid Content ID: {content_id_val}")

    # ── Content Name 校验（必填）──
    content_name = cell("Content Name")
    if not content_name or not str(content_name).strip():
        errors.append("Content Name is required")
        raise _RowSkipError(errors)

    # ── 查找或新建 Content ──
    content: Optional[Content] = None
    is_new = False
    if content_id:
        content = (
            await db.execute(
                select(Content).where(
                    Content.id == content_id,
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalar_one_or_none()
        if content is None:
            # 区分"不存在"与"已丢弃/已删除"：后者明确报错而非静默新建，
            # 避免对同一 Content ID 重复导入产生同名重复内容
            existing_any = (
                await db.execute(select(Content).where(Content.id == content_id))
            ).scalar_one_or_none()
            if existing_any is not None:
                errors.append(
                    f"Content {content_id} is "
                    f"{'deleted' if existing_any.is_deleted else 'discarded'}"
                )
                raise _RowSkipError(errors)

    if not content:
        content = Content(
            content_type=str(cell("Content Type") or ContentType.MOVIE.value).strip() or ContentType.MOVIE.value,
            title=str(content_name).strip(),
            status="None",
        )
        db.add(content)
        await db.flush()
        content_id = content.id
        is_new = True
    else:
        content.title = str(content_name).strip()
        ct_val = cell("Content Type")
        if ct_val:
            content.content_type = str(ct_val).strip()

    # ── 父级关联：按 Parent Name 名称+类型匹配（取第一条）──
    parent_name_raw = cell_optional("Parent Name")
    resolved_parent_id: Optional[int] = None

    # 根据子内容类型确定合法父类型
    if content.content_type == ContentType.EPISODE.value:
        allowed_parent_types = [ContentType.SERIES.value, ContentType.SEASON_SERIES.value]
    elif content.content_type == ContentType.SCHEDULE.value:
        allowed_parent_types = [ContentType.CHANNEL.value]
    elif content.content_type in (ContentType.SEASON_SERIES.value, ContentType.SERIES.value):
        allowed_parent_types = [ContentType.SEASON.value]
    else:
        allowed_parent_types = []

    if parent_name_raw and str(parent_name_raw).strip() and allowed_parent_types:
        parent_name = str(parent_name_raw).strip()
        # 排除已丢弃（is_discarded）内容：挂到被丢弃的父级下 UI 不可见，
        # 同名多条时只匹配未丢弃的；若同名全部已丢弃则视为未找到
        matched_id = (
            await db.execute(
                select(Content.id).where(
                    Content.title == parent_name,
                    Content.content_type.in_(allowed_parent_types),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalars().first()
        if matched_id:
            resolved_parent_id = matched_id
        else:
            errors.append(f"Parent not found by name: {parent_name}")

    if resolved_parent_id is not None:
        content.parent_id = resolved_parent_id

    # ── 基础字段（External ID/Ingest Status/Is Discarded 已从导入模板移除）──
    # Series Type 已从导入模板移除：按 Content Type 推导（1=SERIES/2=SEASON_SERIES/3=SEASON），
    # MOVIE/EPISODE 等其他类型不设置（与旧模板留空行为一致）
    _ct_series_type = {
        ContentType.SERIES.value: 1,
        ContentType.SEASON_SERIES.value: 2,
        ContentType.SEASON.value: 3,
    }
    if content.content_type in _ct_series_type:
        content.series_type = _ct_series_type[content.content_type]

    so_val = cell("Series Ordinal")
    if so_val is not None and str(so_val).strip():
        try:
            content.series_ordinal = int(str(so_val).strip())
        except (ValueError, TypeError):
            errors.append(f"Invalid Series Ordinal: {so_val}")

    seq_val = cell("Sequence")
    if seq_val is not None and str(seq_val).strip():
        try:
            content.sequence = int(str(seq_val).strip())
        except (ValueError, TypeError):
            errors.append(f"Invalid Sequence: {seq_val}")

    # ── Content Type 与 Parent/Series Ordinal/Sequence 必填校验（严格模式）──
    # 业务模型：SEASON（总季）→ SEASON_SERIES（第N季）→ EPISODE（第M集）；
    # SERIES（单季剧）→ EPISODE。只做必填校验，不做禁填校验
    # （多填的字段按上面的解析逻辑原样入库，不报错）。
    # 更新场景（Content ID 已存在）中，已落库的 parent_id/series_ordinal/sequence
    # 允许对应列留空（保持原值不覆盖），仅对"新建"或"原值也为空"的情况强制必填。
    _parent_filled = bool(parent_name_raw and str(parent_name_raw).strip())
    _so_filled = bool(so_val is not None and str(so_val).strip())
    _seq_filled = bool(seq_val is not None and str(seq_val).strip())

    if content.content_type == ContentType.EPISODE.value:
        # 单集：父类（SERIES/SEASON_SERIES）+ Sequence（第几集）必填
        if not _parent_filled and content.parent_id is None:
            errors.append("Parent Name is required when Content Type is EPISODE")
        if not _seq_filled and content.sequence is None:
            errors.append("Sequence is required when Content Type is EPISODE")
    elif content.content_type == ContentType.SEASON_SERIES.value:
        # 某一季：父类（SEASON 总季）+ Series Ordinal（第几季）必填
        if not _parent_filled and content.parent_id is None:
            errors.append("Parent Name is required when Content Type is SEASON_SERIES")
        if not _so_filled and content.series_ordinal is None:
            errors.append("Series Ordinal is required when Content Type is SEASON_SERIES")
    elif content.content_type == ContentType.SCHEDULE.value:
        # 节目单：父类（CHANNEL）必填
        if not _parent_filled and content.parent_id is None:
            errors.append("Parent Name is required when Content Type is SCHEDULE")

    # ── Genre（严格匹配，不创建）──
    genre_ids = _resolve_basic_data_strict(cell("Genre"), genre_cache, "Genre", errors)
    if genre_ids:
        await db.execute(delete(ContentGenre).where(ContentGenre.content_id == content_id))
        for gid in genre_ids:
            db.add(ContentGenre(content_id=content_id, genre_id=gid))

    # 严格匹配失败 → 整行跳过
    if errors:
        raise _RowSkipError(errors)

    # ── Type（严格匹配，不创建）──
    # 标记本行是否新建了元数据记录：流程记录统一在字段全部应用后按必填校验结果写入
    # （避免提前写入 Pending —— 字段尚未应用时评估必然不通过）
    meta_created_this_row = False
    type_name = cell("Type")
    type_id: Optional[int] = None
    if type_name and str(type_name).strip():
        tn = str(type_name).strip()
        type_id = type_cache.get(tn)
        if type_id is None:
            errors.append(f"Type not found: {tn}")
            raise _RowSkipError(errors)
        if content.content_type in movie_type_ids:
            meta = (
                await db.execute(select(ContentMetadata).where(ContentMetadata.content_id == content_id))
            ).scalar_one_or_none()
            if meta:
                meta.type_id = type_id
            else:
                db.add(ContentMetadata(
                    content_id=content_id, type_id=type_id,
                    name=str(content_name).strip(),
                    cdr_id=f"Program_{content_id}",
                ))
                meta_created_this_row = True
        elif content.content_type in series_type_ids:
            meta = (
                await db.execute(select(SeriesMetadata).where(SeriesMetadata.content_id == content_id))
            ).scalar_one_or_none()
            if meta:
                meta.type_id = type_id
            else:
                db.add(SeriesMetadata(
                    content_id=content_id, type_id=type_id,
                    name=str(content_name).strip(),
                    cdr_id=f"Series_{content_id}",
                ))
                meta_created_this_row = True

    # ── Custom Tags（严格匹配，不创建）──
    tag_ids = _resolve_basic_data_strict(cell("Custom Tags"), tag_cache, "Custom Tags", errors)
    if tag_ids:
        await db.execute(delete(ContentCustomTag).where(ContentCustomTag.content_id == content_id))
        for tid in tag_ids:
            db.add(ContentCustomTag(content_id=content_id, custom_tag_id=tid))

    # ── Tags（元数据中的 tag_ids，严格匹配，不创建）──
    tags_str = cell("Tags")
    if tags_str:
        tag_names = [t.strip() for t in str(tags_str).split(",") if t.strip()]
        resolved_tag_ids: list[int] = []
        for tn in tag_names:
            tid = sys_tag_cache.get(tn)
            if tid:
                resolved_tag_ids.append(tid)
            else:
                errors.append(f"Tag not found: {tn}")

        # 严格匹配失败 → 整行跳过
        if errors:
            raise _RowSkipError(errors)

        # 更新元数据表中的 tag_ids 字段
        if resolved_tag_ids:
            if content.content_type in movie_type_ids:
                meta = (
                    await db.execute(select(ContentMetadata).where(ContentMetadata.content_id == content_id))
                ).scalar_one_or_none()
                if meta:
                    meta.tag_ids = resolved_tag_ids
            elif content.content_type in series_type_ids:
                meta = (
                    await db.execute(select(SeriesMetadata).where(SeriesMetadata.content_id == content_id))
                ).scalar_one_or_none()
                if meta:
                    meta.tag_ids = resolved_tag_ids

    # 严格匹配失败 → 整行跳过
    if errors:
        raise _RowSkipError(errors)

    # ── Category（仅匹配）──
    cat_ids = _resolve_relation_ids(cell("Category"), cat_cache, "Category", errors)
    if cat_ids:
        await db.execute(delete(ContentCategory).where(ContentCategory.content_id == content_id))
        for cid in cat_ids:
            db.add(ContentCategory(content_id=content_id, category_id=cid))

    # ── Package（仅匹配）──
    pkg_ids = _resolve_relation_ids(cell("Package"), pkg_cache, "Package", errors)
    if pkg_ids:
        await db.execute(delete(ContentPackage).where(ContentPackage.content_id == content_id))
        for pid in pkg_ids:
            db.add(ContentPackage(content_id=content_id, package_id=pid))

    # ── Provider + Contract/License 列已从导入模板移除：导入只处理元数据，不再创建 Contract/License ──

    # 严格匹配关联字段（Category/Package）失败 → 整行跳过
    if errors:
        raise _RowSkipError(errors)

    # ── 字典字段（全部严格匹配，不创建）──
    vod_type_codes = await resolve_dict_codes_strict(db, "VodType", cell("VodType"), dict_caches, errors, "VodType")
    language_code = await resolve_single_dict_code_strict(db, "Language", cell("Language"), dict_caches, errors, "Language")
    rating_level_code = await resolve_single_dict_code_strict(db, "RatingLevel", cell("RatingLevel"), dict_caches, errors, "RatingLevel")
    advice_codes = await resolve_dict_codes_strict(db, "Advice", cell("Advice"), dict_caches, errors, "Advice")
    audio_lang_codes = await resolve_dict_codes_strict(db, "Language", cell("AudioLang"), dict_caches, errors, "AudioLang")
    subtitle_lang_codes = await resolve_dict_codes_strict(db, "Language", cell("SubtitleLang"), dict_caches, errors, "SubtitleLang")
    metalayout_code = await resolve_single_dict_code_strict(db, "Metalayout", cell("Metalayout"), dict_caches, errors, "Metalayout")

    # 严格匹配字段失败 → 整行跳过（提前退出，避免后续无意义处理）
    if errors:
        raise _RowSkipError(errors)

    # ── 文本/数值字段（直接写入元数据）──
    sort_name = _cell_str(cell("SortName"))
    original_name = _cell_str(cell("OriginalName"))
    original_country = _cell_str(cell("OriginalCountry"))
    short_title = _cell_str(cell("ShortTitle"))
    release_year_raw = cell("ReleaseYear")
    release_year = int(release_year_raw) if release_year_raw not in (None, "") else None
    description = _cell_str(cell("Description"))
    studio = _cell_str(cell("Studio"))
    rating = _cell_str(cell("Rating"))
    begin_duration_raw = cell("BeginDuration")
    begin_duration = int(begin_duration_raw) if begin_duration_raw not in (None, "") else None
    end_duration_raw = cell("EndDuration")
    end_duration = int(end_duration_raw) if end_duration_raw not in (None, "") else None
    # Keywords 为双标识位（与 C2 规范一致）：<平台独占>,<HDR>，取值仅 0/1，最多 2 位
    # 例："1,1" = 平台独占 + HDR 内容；"0,1" = 非独占 + HDR 内容
    keywords_raw = _cell_str(cell("Keywords"))
    keywords = None
    if keywords_raw:
        kw_parts = [k.strip() for k in keywords_raw.split(",") if k.strip()]
        if len(kw_parts) > 2 or any(p not in ("0", "1") for p in kw_parts):
            errors.append(
                f"Invalid Keywords '{keywords_raw}': expected '<exclusive>,<hdr>' with values 0 or 1, e.g. \"1,1\""
            )
        else:
            keywords = kw_parts
    # CdrId/SeriesFlag 已从导入模板移除：cdr_id 按统一规则生成——
    # 每级内容独立前缀 + content 主键（Program_{id} / Series_{id}，与 metadata_service 口径一致）
    cdr_id = (
        f"Series_{content_id}"
        if content.content_type in series_type_ids
        else f"Program_{content_id}"
    )
    # StatusFlag：空值时为 None，不覆盖现有值
    status_flag_raw = _cell_str(cell("StatusFlag"))
    status_flag: Optional[bool] = None
    if status_flag_raw:
        status_flag = status_flag_raw.upper() != "NO"
    series_flag: Optional[int] = None
    sections_info_raw = _cell_str(cell("SectionsInfo"))
    sections_info = None
    if sections_info_raw:
        try:
            sections_info = json.loads(sections_info_raw)
        except (json.JSONDecodeError, ValueError):
            errors.append(f"Invalid SectionsInfo JSON: {sections_info_raw}")
        if sections_info is not None:
            # 按 C2 规范校验并归一化，阻止字符串格式脏数据入库（会导致详情接口 500）
            sections_info, section_errors = normalize_sections_info(sections_info)
            errors.extend(section_errors)

    # ── 写入元数据（ContentMetadata / SeriesMetadata）──
    has_meta_data = any([
        vod_type_codes, language_code, rating_level_code, advice_codes,
        audio_lang_codes, subtitle_lang_codes, metalayout_code,
        sort_name, original_name, original_country, short_title,
        release_year is not None, description, studio, rating,
        begin_duration is not None, end_duration is not None, keywords,
        cdr_id, sections_info,
    ])

    if has_meta_data or content.content_type in movie_type_ids or content.content_type in series_type_ids:
        if content.content_type in movie_type_ids:
            meta = (
                await db.execute(select(ContentMetadata).where(ContentMetadata.content_id == content_id))
            ).scalar_one_or_none()
            if not meta:
                meta = ContentMetadata(
                    content_id=content_id,
                    name=str(content_name).strip(),
                    cdr_id=cdr_id,
                )
                db.add(meta)
                await db.flush()
                meta_created_this_row = True
            _apply_meta_fields(meta, vod_type_codes, language_code, rating_level_code,
                               advice_codes, audio_lang_codes, subtitle_lang_codes, metalayout_code,
                               sort_name, original_name, original_country, short_title, release_year,
                               description, studio, rating,
                               begin_duration, end_duration, keywords, cdr_id, status_flag,
                               sections_info)
        elif content.content_type in series_type_ids:
            meta = (
                await db.execute(select(SeriesMetadata).where(SeriesMetadata.content_id == content_id))
            ).scalar_one_or_none()
            if not meta:
                meta = SeriesMetadata(
                    content_id=content_id,
                    name=str(content_name).strip(),
                    cdr_id=cdr_id,
                )
                db.add(meta)
                await db.flush()
                meta_created_this_row = True
            _apply_meta_fields(meta, vod_type_codes, language_code, rating_level_code,
                               advice_codes, audio_lang_codes, subtitle_lang_codes, metalayout_code,
                               sort_name, original_name, original_country, short_title, release_year,
                               description, studio, rating,
                               begin_duration, end_duration, keywords, cdr_id, status_flag,
                               sections_info)

        # 导入属于同步链路：字段全部应用后按必填规则实时校验，
        # 通过写 Passed（绿勾），不通过写 Pending（红叉提示补全）
        if meta_created_this_row:
            from app.internal.cms_biz_orchestration.services.metadata_validation_service import check_metadata_complete
            from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
            meta_complete, _meta_missing = await check_metadata_complete(
                db, content_id, content.content_type,
            )
            await complete_process_and_update_status(
                db, content_id=content_id, content_type=content.content_type,
                process_name="Metadata", processed_by=processed_by or "import",
                record_status="Passed" if meta_complete else "Pending",
                info="Excel导入",
            )

    if errors:
        raise _RowSkipError(errors)

    # 更新已存在内容（非新建）时：
    # Excel 导入覆盖了节点数据（主表字段/元数据/题材/标签/栏目/服务包等），
    # 已发布/准备发布等内容需回退状态重新走审核。
    # （Ingest Status 列已从导入模板移除，不再支持显式指定状态跳过回退）
    if not is_new:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        logger.info(f"[VOD导入] 更新已有内容 #{content_id}，执行已发布编辑回退检查")
        await rollback_after_published_edit(
            db, content_id, content.content_type, processed_by or "import", "Excel导入更新内容"
        )

    # 新建内容时自动创建 arrangement 任务，与手动创建路径（create_content）对齐。
    # 否则导入产出的内容在 task 表无记录，内容管理页"分配"功能会提示无待分配任务。
    if is_new:
        from app.internal.cms_biz_package.services import task_service
        await task_service.create_arrangement_task(db, content.id, processed_by=processed_by or "import")

    # ── 继承父内容许可证（与 create_content 对齐，bug 32459）──
    # 导入的子内容（SEASON_SERIES/SERIES/EPISODE）自动继承父内容的许可证关联，
    # 避免导入后子内容缺少许可证导致后续审核/分发流程受阻。
    # 注意：
    # 1. 此处理仅补齐缺失的许可证，不删除子内容已有的许可证关联；
    # 2. 新建（is_new）与更新（重复导入同一 Content ID）分支均执行继承——
    #    首次导入时父级可能尚未绑定许可证，若仅在新建时继承，
    #    重复导入将永远无法补齐父级后续绑定的许可证（bug：导入的vod内容未自动继承上级的许可证）。
    if content.parent_id and content.content_type in (
        ContentType.SEASON_SERIES.value,
        ContentType.SERIES.value,
        ContentType.EPISODE.value,
    ):
        parent_license_ids = (
            await db.execute(
                select(LicenseContent.license_id).where(
                    LicenseContent.content_id == content.parent_id,
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        if parent_license_ids:
            existing_license_ids = set(
                (
                    await db.execute(
                        select(LicenseContent.license_id).where(
                            LicenseContent.content_id == content_id,
                        )
                    )
                ).scalars().all()
            )
            added_license_ids: list[int] = []
            for lid in parent_license_ids:
                if lid in existing_license_ids:
                    continue
                db.add(LicenseContent(license_id=lid, content_id=content_id))
                added_license_ids.append(lid)
            if added_license_ids:
                logger.info(
                    f"[VOD导入] 继承父内容 #{content.parent_id} 许可证: {added_license_ids} → 子内容 #{content_id}"
                )

    # ── Activity Log：导入创建/更新均记录（与节目单导入同模式，随行级事务回滚）──
    from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
    await write_log(
        db,
        user_id=None,
        user_name=processed_by,
        operation_type=OperationType.CONTENT_CREATE if is_new else OperationType.CONTENT_EDIT,
        operation_object_code="OBJ_CONTENT", operation_object_params={"name": content.title},
        operation_content_code="LOG_CONTENT_CREATE" if is_new else "LOG_CONTENT_EDIT",
        operation_content_params={"title": content.title},
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        result="success",
    )

    return (True, is_new)


def generate_vod_import_template() -> bytes:
    """生成 VOD 内容导入模板 Excel 文件（32 列，严格模式）。

    - 必填字段表头带红色 (*) 标记（导入时按表头名称匹配，(*) 会被忽略）
    - 含两行示例数据（电影 / 系列）及填写说明（Instructions 工作表）
    """
    import openpyxl
    from openpyxl.cell.rich_text import CellRichText, TextBlock
    from openpyxl.cell.text import InlineFont

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "VOD Contents"

    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)
    # 富文本字体：字段名白色 + (*) 红色（写在同一单元格）
    base_inline = InlineFont(rFont="Calibri", b=True, color="FFFFFF")
    mark_inline = InlineFont(rFont="Calibri", b=True, color="FF0000")

    for col, header in enumerate(VOD_IMPORT_HEADERS, 1):
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

    # 列宽（32 列）
    col_widths = [
        12, 14, 30, 24, 14, 12,                   # Content 主表（6）
        20, 16, 20, 20, 20, 20,                    # 关联（6）
        20, 16, 16, 20,                            # 元数据-字典
        20, 20, 16, 16, 12,                        # 元数据-名称
        40, 20,                                    # 元数据-描述
        12,                                        # 元数据-评分（1）
        20, 20, 12, 12,                            # 元数据-音视频
        20, 16, 12,                                # 元数据-标识（3）
        40,                                        # 元数据-章节
    ]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    # 示例数据行
    example_rows = [
        # 示例 1：电影（MOVIE）——必填字段全部填写
        # Keywords 格式：<平台独占>,<HDR>，取值 0/1（如 "1,1" = 平台独占 + HDR 内容）
        [
            "", "MOVIE", "Sample Movie", "",
            "", "",
            "Action,Adventure", "Movie", "", "Tag1,Tag2", "Movies", "Basic Package",
            "Film", "English", "PG", "Violence",
            "Sample Movie", "Sample Movie Original", "US", "Sample", "2025",
            "A sample movie description", "Sample Studio",
            "8.5",
            "English", "English", "0", "7200",
            "1,1", "M0", "YES",
            "[{\"type\": 3, \"action\": 0, \"tag\": \"chapter\", \"start\": 0, \"end\": 1800}]",
        ],
        # 示例 2：系列（SERIES）——系列类型另需 OriginalName 必填（Series Type 已移除，由 Content Type 推导）
        [
            "", "SERIES", "Sample Series", "",
            "", "",
            "Drama", "Series", "", "", "", "",
            "Serial", "English", "PG", "",
            "Sample Series", "Sample Series Original", "US", "", "2025",
            "A sample series description", "",
            "",
            "", "", "", "",
            "0,1", "M0", "YES",
            "",
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
        "1. Fields marked with a red (*) are required: Content Name, Genre, Type, VodType, RatingLevel, Metalayout; "
        "OriginalName is also required when Content Type is SERIES/SEASON_SERIES/SEASON. "
        "If any row is missing a required field, the entire file will be rejected.",
        "2. Content ID: If the ID already exists, that content will be updated (overwritten with the latest data); "
        "if left empty, a new content will be created.",
        "3. Content Type options: MOVIE / SERIES / SEASON_SERIES / SEASON / EPISODE / CHANNEL / SCHEDULE "
        "(defaults to MOVIE if left empty).",
        "4. Parent Name / Series Ordinal / Sequence depend on Content Type (the Series Type column has been removed; "
        "it is derived automatically from Content Type):",
        "   - EPISODE: Parent Name (a SERIES or SEASON_SERIES) and Sequence (episode number) are required.",
        "   - SEASON_SERIES: Parent Name (a SEASON, the season group) and Series Ordinal (season number) are required.",
        "   - SERIES / SEASON / MOVIE: Parent Name, Series Ordinal and Sequence are optional.",
        "   (When updating an existing content, a column left empty keeps its current value.)",
        "5. Genre/Type/Tags/Custom Tags/Category/Package/VodType/Language/RatingLevel/Advice/AudioLang/SubtitleLang/Metalayout "
        "must match existing data in the system (separate multiple values with commas); "
        "rows that fail to match will be skipped.",
        "6. StatusFlag: YES or NO (left empty = keep the existing value).",
        "7. Keywords format: two flags separated by a comma — <exclusive>,<HDR>. "
        "Exclusive: 0 = Non-platform exclusive, 1 = Only Tivibu (platform exclusive); "
        "HDR: 0 = Non-HDR content, 1 = HDR content. "
        "Examples: \"1,1\" = platform exclusive HDR content; \"0,1\" = non-exclusive HDR content; "
        "\"1\" or \"1,0\" = platform exclusive non-HDR content.",
        "8. SectionsInfo is a JSON array, e.g. [{\"type\": 3, \"action\": 0, \"tag\": \"chapter\", \"start\": 0, \"end\": 1800}]. "
        "type: 1=intro/2=ad/3=chapter; action: 0=no skip/1=skip; tag is the label text; start/end are integer seconds.",
        "9. This template imports metadata only: the columns External ID/Ingest Status/Is Discarded/Provider/License/"
        "Unpublish Date/Publish Date/CdrId/SeriesFlag/RatingType/RatingId/Created At/Updated At have been removed, "
        "and importing no longer creates Contracts/Licenses.",
        "10. Strict mode: missing or unexpected columns (e.g. the removed columns above, or the Series Type column "
        "in old files) will cause the entire file to be rejected.",
    ]
    for row_idx, note in enumerate(notes, 1):
        notes_ws.cell(row=row_idx, column=1, value=note)
    notes_ws.column_dimensions["A"].width = 120
    notes_ws["A1"].font = Font(bold=True)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()
