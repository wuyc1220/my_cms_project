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

from datetime import date
from typing import Optional

from loguru import logger
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException

from app.internal.cms_biz_metada.models.basic import Genre, CustomTag, Category, Picture, ContentType as ContentTypeModel
from app.internal.cms_biz_package.models import ContentType, ContentStatus
from app.internal.cms_biz_package.models.package import Content, ContentPackage, Package, ContentCustomTag, ContentCategory
from app.internal.cms_biz_package.models.task import Task
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_scp.models.trade import Contract, License, LicenseContent, Provider
from app.internal.cms_biz_orchestration.models.content_metadata import ContentMetadata, SeriesMetadata
from app.internal.cms_biz_orchestration.services.storage import storage_service
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


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _get_genre_name(db: AsyncSession, genre_id: Optional[int]) -> Optional[str]:
    """查询题材名称。"""
    if genre_id is None:
        return None
    row = (await db.execute(select(Genre.name).where(Genre.id == genre_id, Genre.is_deleted.is_(False)))).scalar_one_or_none()
    return row


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
    lic_ids = (
        await db.execute(
            select(LicenseContent.license_id).where(
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
            )
        )
    ).scalars().all()

    if not lic_ids:
        return 0, None, None

    rows = (
        await db.execute(
            select(License.start_date, License.end_date).where(
                License.id.in_(lic_ids),
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
    genre_name = await _get_genre_name(db, c.genre_id)

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
    if c.content_type in (ContentType.SERIES.value, ContentType.SEASON.value):
        child_type = ContentType.EPISODE.value if c.content_type == ContentType.SERIES.value else ContentType.SERIES.value
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

    return ContentListItem(
        id=c.id,
        content_type=c.content_type,
        title=c.title,
        status=c.status,
        parent_id=c.parent_id,
        parent_title=parent_title,
        genre_id=c.genre_id,
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
    )


# ─── Content CRUD ─────────────────────────────────────────────────────

async def list_contents(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    content_id: Optional[int] = None,
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
        content_id          内容 id（精确匹配）
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
    logger.info(f"list_contents 入参: page={page}, page_size={page_size}, content_id={content_id}, title={title}, content_types={content_types}, statuses={statuses}, genre_ids={genre_ids}, custom_tag_ids={custom_tag_ids}, parent_id={parent_id}, created_from={created_from}, created_to={created_to}, without_license={without_license}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, is_discarded={is_discarded}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Content).where(Content.is_deleted.is_(False))

    if is_discarded is not None:
        query = query.where(Content.is_discarded.is_(is_discarded))
    else:
        query = query.where(Content.is_discarded.is_(False))

    # 数据权限过滤：admin 不过滤；其他用户只能看到自己创建的 + 被数据权限授权的内容
    query = await apply_content_data_auth(db, current_user, query)

    if content_id is not None and title:
        query = query.where(or_(Content.id == content_id, Content.title.ilike(f"%{title}%")))
    elif content_id is not None:
        query = query.where(Content.id == content_id)
    elif title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if statuses:
        query = query.where(Content.status.in_(statuses))
    if genre_ids:
        query = query.where(Content.genre_id.in_(genre_ids))
    if custom_tag_ids:
        sub_ct = select(ContentCustomTag.content_id).where(
            ContentCustomTag.custom_tag_id.in_(custom_tag_ids)
        )
        query = query.where(Content.id.in_(sub_ct))
    if parent_id is not None:
        query = query.where(Content.parent_id == parent_id)
    if created_from:
        query = query.where(Content.created_at >= created_from)
    if created_to:
        query = query.where(Content.created_at <= created_to + " 23:59:59")

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
) -> AdjacentContentResponse:
    """
    查询当前内容的上一条/下一条内容 ID（按 id 排序）。

    输入：
        content_id   当前内容 id
        current_user 当前用户（用于数据权限过滤）
    输出：
        AdjacentContentResponse（prev_id / next_id）
    """
    logger.info(f"get_adjacent_content 入参: content_id={content_id}")
    await _get_content_or_404(db, content_id)

    query = select(Content.id).where(Content.is_deleted.is_(False), Content.is_discarded.is_(False))
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
            )
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


async def create_content(db: AsyncSession, data: ContentCreate) -> ContentListItem:
    """
    新建内容。

    输入：ContentCreate
    输出：ContentListItem（返回主节点信息）

    业务规则：
    - EPISODE：校验 parent_id 指向有效 SERIES；设置 sequence
    - SERIES  ：自动按 volumn_count 创建 EPISODE 子节点
    - SEASON  ：按 season_details 自动创建 SERIES 子节点，再为每个 SERIES 创建 EPISODE
    - SCHEDULE：校验 parent_id 指向有效 CHANNEL；设置 begin_time/end_time
    """
    logger.info(f"create_content 入参: data={data}")
    ctype = data.content_type.upper()

    # ── Bug 31470: 校验内容名称唯一性（同一 content_type + parent_id 下不能重名）─────────
    existing_name = (
        await db.execute(
            select(Content.id).where(
                Content.title == data.title,
                Content.content_type == ctype,
                Content.parent_id == data.parent_id if data.parent_id else Content.parent_id.is_(None),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()
    if existing_name:
        raise BusinessException(ErrorCode.CONTENT_NAME_EXISTS, get_msg("CONTENT_NAME_EXISTS"))

    # ── 校验父级节点 ─────────────────────────────────────────────────
    if ctype == ContentType.EPISODE.value:
        if not data.parent_id:
            raise BusinessException(ErrorCode.EPISODE_REQUIRES_SERIES, get_msg("EPISODE_REQUIRES_SERIES"))
        parent = await _get_content_or_404(db, data.parent_id)
        if parent.content_type != ContentType.SERIES.value:
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

    elif ctype == ContentType.SERIES.value:
        if data.parent_id and data.series_ordinal is not None:
            # 校验季序号是否已存在（在父 SEASON 下）
            existing = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == data.parent_id,
                        Content.content_type == ContentType.SERIES.value,
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

    # ── 创建主节点 ───────────────────────────────────────────────────
    series_type_val: Optional[int] = None
    if ctype == ContentType.SERIES.value:
        series_type_val = data.series_type if data.series_type else 1  # 默认普通连续剧
    elif ctype == ContentType.SEASON.value:
        series_type_val = 3  # 总季

    main_content = Content(
        content_type=ctype,
        title=data.title,
        status=ContentStatus.NONE.value,
        parent_id=data.parent_id,
        series_type=series_type_val,
        genre_id=data.genre_id,
        sequence=data.sequence if ctype == ContentType.EPISODE.value else None,
        series_ordinal=data.series_ordinal if ctype == ContentType.SERIES.value else None,
        begin_time=data.begin_time if ctype == ContentType.SCHEDULE.value else None,
        end_time=data.end_time if ctype == ContentType.SCHEDULE.value else None,
    )
    db.add(main_content)
    await db.flush()  # 获取 main_content.id

    # ── 自动创建子节点（SERIES → EPISODE）───────────────────────────
    if ctype == ContentType.SERIES.value and data.volumn_count and data.volumn_count > 0:
        for seq in range(1, data.volumn_count + 1):
            ep = Content(
                content_type=ContentType.EPISODE.value,
                title=f"{data.title} E{seq:02d}",
                status=ContentStatus.NONE.value,
                parent_id=main_content.id,
                genre_id=data.genre_id,
                sequence=seq,
            )
            db.add(ep)
        
        # 记录 InjectSubContent 流程节点状态
        from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
        await complete_process_and_update_status(
            db,
            content_id=main_content.id,
            content_type=ctype,
            process_name="InjectSubContent",
            processed_by="system",
            info=f"批量创建 {data.volumn_count} 个 EPISODE 子节点",
        )

    # ── 自动创建子节点（SEASON → SERIES → EPISODE）──────────────────
    elif ctype == ContentType.SEASON.value and data.season_details:
        for detail in data.season_details:
            series_child = Content(
                content_type=ContentType.SERIES.value,
                title=f"{data.title} S{detail.series_ordinal:02d}",
                status=ContentStatus.NONE.value,
                parent_id=main_content.id,
                series_type=2,   # 单季
                genre_id=data.genre_id,
                series_ordinal=detail.series_ordinal,
            )
            db.add(series_child)
            await db.flush()

            for seq in range(1, detail.episode_count + 1):
                ep = Content(
                    content_type=ContentType.EPISODE.value,
                    title=f"{data.title} S{detail.series_ordinal:02d}E{seq:02d}",
                    status=ContentStatus.NONE.value,
                    parent_id=series_child.id,
                    genre_id=data.genre_id,
                    sequence=seq,
                )
                db.add(ep)
        
        # 记录 InjectSubContent 流程节点状态
        from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
        await complete_process_and_update_status(
            db,
            content_id=main_content.id,
            content_type=ctype,
            process_name="InjectSubContent",
            processed_by="system",
            info=f"批量创建 {len(data.season_details)} 个 SEASON 子节点",
        )

    await db.flush()

    # ── 保存自定义标签关联 ─────────────────────────────────────────
    if data.custom_tag_ids:
        for tag_id in data.custom_tag_ids:
            assoc = ContentCustomTag(content_id=main_content.id, custom_tag_id=tag_id)
            db.add(assoc)

        # 将自定义标签传播到自动创建的子节点
        if ctype == ContentType.SERIES.value:
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
                        Content.content_type == ContentType.SERIES.value,
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

    await db.commit()
    await db.refresh(main_content)

    # ── 为子内容注入记录流程状态 ──────────────────────────────────
    # 当创建 EPISODE（作为 SERIES 的子节点）或 SERIES（作为 SEASON 的子节点）时，
    # 需要记录父节点的 InjectSubContent 流程状态
    if ctype == ContentType.EPISODE.value and data.parent_id:
        # 检查父节点是否为 SERIES
        parent = await _get_content_or_404(db, data.parent_id)
        if parent.content_type == ContentType.SERIES.value:
            from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
            await complete_process_and_update_status(
                db,
                content_id=data.parent_id,
                content_type=parent.content_type,
                process_name="InjectSubContent",
                processed_by="system",
                info=f"注入 EPISODE 子节点: {data.title}",
            )
    
    elif ctype == ContentType.SERIES.value and data.parent_id:
        # 检查父节点是否为 SEASON
        parent = await _get_content_or_404(db, data.parent_id)
        if parent.content_type == ContentType.SEASON.value:
            from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status
            await complete_process_and_update_status(
                db,
                content_id=data.parent_id,
                content_type=parent.content_type,
                process_name="InjectSubContent",
                processed_by="system",
                info=f"注入 SERIES 子节点: {data.title}",
            )

    # 自动创建 arrangement 任务
    from app.internal.cms_biz_package.services import task_service
    await task_service.create_arrangement_task(
        db, main_content.id, assignee_id=data.assignee_id, processed_by="system"
    )
    
    # 为自动创建的子内容也创建 arrangement 任务
    if ctype == ContentType.SERIES.value and data.volumn_count and data.volumn_count > 0:
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
        # 为 SERIES 子内容创建任务
        series_children = (
            await db.execute(
                select(Content).where(
                    Content.parent_id == main_content.id,
                    Content.content_type == ContentType.SERIES.value,
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

    if data.title is not None:
        c.title = data.title
    if data.genre_id is not None:
        c.genre_id = data.genre_id
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

    await db.commit()
    await db.refresh(c)
    return await _build_item(db, c)


async def delete_content(db: AsyncSession, content_id: int) -> None:
    """
    软删除内容（is_deleted=True）。

    输入：content_id
    业务规则：
        - 存在子内容时不允许删除，需先删除子内容
    """
    logger.info(f"delete_content 入参: content_id={content_id}")
    c = await _get_content_or_404(db, content_id)
    
    if c.status == "Published":
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
    
    c.previous_status = c.status
    c.is_discarded = True
    await db.execute(
        update(LicenseContent)
        .where(LicenseContent.content_id == content_id, LicenseContent.is_deleted.is_(False))
        .values(is_deleted=True)
    )
    await db.commit()


async def batch_delete_contents(db: AsyncSession, content_ids: list[int]) -> int:
    """
    批量软删除内容。

    输入：content_ids
    业务规则：
        - 存在子内容的内容不允许删除，跳过
    返回：成功删除的数量
    """
    logger.info(f"batch_delete_contents 入参: content_ids={content_ids}")
    deleted_count = 0
    
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
            
            c.previous_status = c.status
            c.is_discarded = True
            await db.execute(
                update(LicenseContent)
                .where(LicenseContent.content_id == content_id, LicenseContent.is_deleted.is_(False))
                .values(is_deleted=True)
            )
            deleted_count += 1
        except NotFoundException:
            logger.warning(f"batch_delete_contents: content_id={content_id} not found, skip")
            continue
    
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
    """根据内容类型获取 Picture 的 entity_type。"""
    mapping = {
        "MOVIE": "program",
        "EPISODE": "program",
        "SERIES": "series",
        "SEASON": "season",
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

    rows = (
        await db.execute(
            select(LicenseContent.license_id).where(
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
            )
        )
    ).scalars().all()

    if not rows:
        return []

    from app.internal.cms_biz_scp.models.trade import Contract, Provider, LicensePlatform

    result: list[ContentLicenseRef] = []
    for lid in rows:
        lic_row = (
            await db.execute(
                select(License).where(License.id == lid, License.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if not lic_row:
            continue
        contract = lic_row.contract
        provider_name = ""
        provider_id = 0
        if contract:
            provider_id = contract.provider_id
            if contract.provider:
                provider_name = contract.provider.name

        # 查询许可证关联的平台
        platform_rows = (
            await db.execute(
                select(LicensePlatform.platform, LicensePlatform.ad_rights)
                .where(LicensePlatform.license_id == lid, LicensePlatform.is_deleted.is_(False))
            )
        ).all()
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
        Content.content_type == ContentType.SERIES.value,
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    # 数据权限过滤：admin 不过滤；其他用户仅看到自己有权限的 SERIES
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

_VOD_TYPES = [ContentType.MOVIE.value, ContentType.SEASON.value, ContentType.SERIES.value, ContentType.EPISODE.value]


async def _build_vod_item(db: AsyncSession, c: Content) -> VodContentListItem:
    """将 ORM Content 转换为 VOD 列表响应，附加服务包名称和供应商名称。"""
    genre_name = await _get_genre_name(db, c.genre_id)

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
    entity_type = "program" if c.content_type in (ContentType.MOVIE.value, ContentType.EPISODE.value) else "series"
    # 同时匹配小写和大写的 entity_type
    entity_types = [entity_type, entity_type.capitalize()]
    pic = (
        await db.execute(
            select(Picture.file_path)
            .where(
                Picture.entity_type.in_(entity_types),
                Picture.entity_id == c.id,
                Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
            )
            .order_by(Picture.created_at)
            .limit(1)
        )
    ).scalar_one_or_none()
    if pic:
        poster_url = storage_service.get_file_url(pic)

    # 查询发布日期和下架日期（从 publish_task 表获取）
    publish_date: Optional[str] = None
    takedown_date: Optional[str] = None

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
        publish_date = publish_task_result.strftime("%Y-%m-%d %H:%M:%S")

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
        takedown_date = unpublish_task_result.strftime("%Y-%m-%d %H:%M:%S")

    return VodContentListItem(
        id=c.id,
        content_type=c.content_type,
        title=c.title,
        status=c.status,
        genre_id=c.genre_id,
        genre_name=genre_name,
        type_name=type_name,
        category_name=category_name,
        takedown_date=takedown_date,
        publish_date=publish_date,
        poster_url=poster_url,
        package_names=package_names,
        provider_names=provider_names,
        license_start=license_start,
        license_end=license_end,
        created_at=c.created_at,
    )


async def list_vod_contents(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    title: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    statuses: Optional[list[str]] = None,
    genre_id: Optional[int] = None,
    provider_id: Optional[int] = None,
    package_name: Optional[str] = None,
    license_start_from: Optional[str] = None,
    license_start_to: Optional[str] = None,
    license_end_from: Optional[str] = None,
    license_end_to: Optional[str] = None,
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
        genre_id            题材 id
        provider_id         供应商 id（通过 license_content→license→contract 关联）
        package_name        服务包名称关键字（通过 content_package 关联）
        license_start_from  许可证开始日期范围下限（YYYY-MM-DD）
        license_start_to    许可证开始日期范围上限
        license_end_from    许可证结束日期范围下限
        license_end_to      许可证结束日期范围上限

    输出：
        PaginatedResponse[VodContentListItem]

    过滤规则：
        - content_type IN (MOVIE/SEASON/SERIES/EPISODE)
        - is_archived = false（仅普通内容，归档内容由归档管理模块负责）
    """
    logger.info(f"list_vod_contents 入参: page={page}, page_size={page_size}, title={title}, content_types={content_types}, statuses={statuses}, genre_id={genre_id}, provider_id={provider_id}, package_name={package_name}, license_start_from={license_start_from}, license_start_to={license_start_to}, license_end_from={license_end_from}, license_end_to={license_end_to}, sort_by={sort_by}, sort_order={sort_order}")
    # 限定为 VOD 内容类型
    allowed = set(_VOD_TYPES)
    if content_types:
        allowed = allowed & set(content_types)
    effective_types = list(allowed) if allowed else _VOD_TYPES

    query = select(Content).where(
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
        Content.is_archived.is_(False),
        Content.content_type.in_(effective_types),
    )
    # 数据权限过滤：admin 不过滤；其他用户仅看自己创建的或被授权的 VOD 内容
    query = await apply_content_data_auth(db, current_user, query)

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if statuses:
        # 注意：Content.status 默认是字符串 "None"，不是数据库 NULL
        query = query.where(Content.status.in_(statuses))
    if genre_id is not None:
        query = query.where(Content.genre_id == genre_id)

    # 供应商过滤：找出该供应商下的所有许可证 id，再找关联的 content id
    if provider_id is not None:
        contract_ids = (
            await db.execute(
                select(Contract.id).where(
                    Contract.provider_id == provider_id,
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

    # 服务包名称过滤
    if package_name:
        pkg_ids = (
            await db.execute(
                select(Package.id).where(
                    Package.name.ilike(f"%{package_name}%"),
                    Package.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        content_ids_from_pkg = (
            await db.execute(
                select(ContentPackage.content_id).where(
                    ContentPackage.package_id.in_(pkg_ids)
                )
            )
        ).scalars().all()
        query = query.where(Content.id.in_(content_ids_from_pkg))

    # 许可证日期过滤
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
        content_ids_from_lic = (
            await db.execute(
                select(LicenseContent.content_id).where(
                    LicenseContent.license_id.in_(lic_query),
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        query = query.where(Content.id.in_(content_ids_from_lic))

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

    items = [await _build_vod_item(db, c) for c in contents]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)
