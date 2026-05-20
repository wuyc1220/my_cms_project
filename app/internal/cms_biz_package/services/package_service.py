"""
服务包（Package）业务逻辑层。

职责：
- Package 的 CRUD（新增、查询、编辑、软删除、批量软删除）
- 服务包平台关系维护（PackagePlatform）
- 服务包内容关联管理（ContentPackage）：新增关联、移除关联、查询已关联内容
- 查询可供选择的内容列表（Add Content to Package 弹框数据源）

业务规则：
1. 服务包名称全局唯一，重复时抛出 400 错误
2. 更新平台时，先删除旧记录再批量插入新记录
3. 同一内容不能重复添加至同一服务包（唯一约束保证，捕获后抛出 400）
4. 软删除：is_deleted=True，不物理删除
"""

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.package import Content, ContentPackage, Package, PackagePlatform
from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_package.schemas.package import (
    ContentSimpleItem,
    PackageContentAddRequest,
    PackageCreate,
    PackageListItem,
    PackageUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


# ─── 内部辅助 ─────────────────────────────────────────────────────────

def _to_list_item(package: Package) -> PackageListItem:
    """将 ORM 对象转换为响应 schema。"""
    return PackageListItem(
        id=package.id,
        name=package.name,
        package_type=package.package_type,
        platforms=[p.platform for p in package.platforms],
        description=package.description,
        ingest_status=package.ingest_status,
        created_at=package.created_at,
    )


async def _get_package_or_404(db: AsyncSession, package_id: int) -> Package:
    """查询服务包，不存在则抛 404。"""
    result = await db.execute(
        select(Package).where(Package.id == package_id, Package.is_deleted.is_(False))
    )
    package = result.scalar_one_or_none()
    if not package:
        raise NotFoundException(ErrorCode.PACKAGE_NOT_FOUND, get_msg("PACKAGE_NOT_FOUND"))
    return package


async def _check_name_unique(db: AsyncSession, name: str, exclude_id: int | None = None) -> None:
    """校验服务包名称唯一性。"""
    query = select(Package.id).where(Package.name == name, Package.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(Package.id != exclude_id)
    existing = (await db.execute(query.limit(1))).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS"))


# ─── Package CRUD ─────────────────────────────────────────────────────

async def list_packages(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    package_type: str | None = None,
    platforms: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
    description: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[PackageListItem]:
    """
    查询服务包列表（分页）。

    输入参数：
        page            当前页码（从 1 开始）
        page_size       每页条数
        name            服务包名称关键字（模糊匹配）
        package_type    服务包类型（精确匹配）
        platforms       平台列表（任一匹配）
        ingest_statuses Ingest 状态列表（任一匹配）
        description     描述关键字（模糊匹配）

    输出：
        PaginatedResponse[PackageListItem]
    """
    logger.info(f"list_packages 入参: page={page}, page_size={page_size}, name={name}, package_type={package_type}, platforms={platforms}, ingest_statuses={ingest_statuses}, description={description}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Package).where(Package.is_deleted.is_(False))

    if name:
        query = query.where(Package.name.ilike(f"%{name}%"))
    if package_type:
        query = query.where(Package.package_type == package_type)
    if ingest_statuses:
        query = query.where(Package.ingest_status.in_(ingest_statuses))
    if description:
        query = query.where(Package.description.ilike(f"%{description}%"))
    if platforms:
        # 过滤至少包含 platforms 列表中任一平台的服务包
        from sqlalchemy import exists
        platform_sub = select(PackagePlatform.package_id).where(
            PackagePlatform.platform.in_(platforms)
        )
        query = query.where(Package.id.in_(platform_sub))

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Package, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Package.id.desc())
        else:
            query = query.order_by(Package.id.desc())
    else:
        query = query.order_by(Package.id.desc())

    packages = (
        await db.execute(
            query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    items = [_to_list_item(pkg) for pkg in packages]
    logger.info(f"list_packages 出参: total={total}, page={page}, items_count={len(items)}")
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )


async def get_package(db: AsyncSession, package_id: int) -> Package:
    """
    查询单个服务包。

    输入参数：
        package_id  服务包 id
    输出：
        Package ORM 对象
    异常：
        404 服务包不存在
    """
    logger.info(f"get_package 入参: package_id={package_id}")
    return await _get_package_or_404(db, package_id)


async def create_package(db: AsyncSession, data: PackageCreate) -> Package:
    """
    新建服务包。

    输入参数：
        data    PackageCreate（name/package_type/platforms/description）
    输出：
        Package ORM 对象
    业务规则：
        - 名称不能重复
        - 同步写入 PackagePlatform 关联记录
    """
    logger.info(f"create_package 入参: data={data}")
    await _check_name_unique(db, data.name)

    package = Package(
        name=data.name,
        package_type=data.package_type,
        description=data.description,
    )
    db.add(package)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS"))

    for platform in data.platforms:
        db.add(PackagePlatform(package_id=package.id, platform=platform))

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS"))
    await db.refresh(package)
    logger.info(f"创建服务包成功: id={package.id}, name={package.name}")
    return package


async def update_package(db: AsyncSession, package_id: int, data: PackageUpdate) -> Package:
    """
    更新服务包。

    输入参数：
        package_id  服务包 id
        data        PackageUpdate（所有字段均可选）
    输出：
        更新后的 Package ORM 对象
    业务规则：
        - 若修改名称则重新校验唯一性
        - 若修改平台则删除旧 PackagePlatform 后批量插入新记录
    """
    logger.info(f"update_package 入参: package_id={package_id}, data={data}")
    package = await _get_package_or_404(db, package_id)

    if data.name is not None and data.name != package.name:
        await _check_name_unique(db, data.name, exclude_id=package_id)
        package.name = data.name
    if data.package_type is not None:
        package.package_type = data.package_type
    if data.description is not None:
        package.description = data.description
    if data.ingest_status is not None:
        package.ingest_status = data.ingest_status

    if data.platforms is not None:
        # 删除旧平台记录，重新写入
        old_platforms = (
            await db.execute(select(PackagePlatform).where(
                PackagePlatform.package_id == package_id,
                PackagePlatform.is_deleted.is_(False)
            ))
        ).scalars().all()
        for old in old_platforms:
            await db.delete(old)
        await db.flush()
        for platform in data.platforms:
            db.add(PackagePlatform(package_id=package_id, platform=platform))

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS"))
    await db.refresh(package)
    return package


async def delete_package(db: AsyncSession, package_id: int) -> None:
    """
    软删除服务包（is_deleted=True）。

    输入参数：
        package_id  服务包 id
    """
    logger.info(f"delete_package 入参: package_id={package_id}")
    package = await _get_package_or_404(db, package_id)
    package.is_deleted = True
    await db.commit()


async def batch_delete_packages(db: AsyncSession, req: BatchDeleteRequest) -> int:
    """
    批量软删除服务包。

    输入参数：
        req.ids     要删除的服务包 id 列表
    输出：
        实际删除数量
    """
    logger.info(f"batch_delete_packages 入参: req={req}")
    packages = (
        await db.execute(
            select(Package).where(Package.id.in_(req.ids), Package.is_deleted.is_(False))
        )
    ).scalars().all()
    for pkg in packages:
        pkg.is_deleted = True
    await db.commit()
    return len(packages)


# ─── Package↔Content 关联 ─────────────────────────────────────────────

async def list_package_contents(
    db: AsyncSession,
    package_id: int,
    page: int = 1,
    page_size: int = 10,
) -> PaginatedResponse[ContentSimpleItem]:
    """
    查询服务包已关联的内容列表（分页）。

    输入参数：
        package_id  服务包 id
        page        页码
        page_size   每页条数
    输出：
        PaginatedResponse[ContentSimpleItem]
    """
    logger.info(f"list_package_contents 入参: package_id={package_id}, page={page}, page_size={page_size}")
    await _get_package_or_404(db, package_id)

    base_query = (
        select(ContentPackage)
        .where(ContentPackage.package_id == package_id)
    )

    count_result = await db.execute(select(func.count()).select_from(base_query.subquery()))
    total = count_result.scalar_one()

    rows = (
        await db.execute(
            base_query.order_by(ContentPackage.allocated_at.asc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    # 收集所有 genre_id 用于批量查询
    genre_ids: set[int] = set()
    content_rows: list[tuple[ContentPackage, Content]] = []
    for row in rows:
        c = row.content
        if c and not c.is_deleted:
            content_rows.append((row, c))
            if c.genre_id:
                genre_ids.add(c.genre_id)

    # 批量查询 genre 名称
    genre_map: dict[int, str] = {}
    if genre_ids:
        genre_result = await db.execute(select(Genre).where(Genre.id.in_(genre_ids), Genre.is_deleted.is_(False)))
        genre_map = {g.id: g.name for g in genre_result.scalars().all()}

    items: list[ContentSimpleItem] = []
    for row, c in content_rows:
        items.append(ContentSimpleItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            genre=genre_map.get(c.genre_id) if c.genre_id else None,
            status=c.status,
            has_license=False,
        ))

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )


async def add_contents_to_package(
    db: AsyncSession,
    package_id: int,
    req: PackageContentAddRequest,
) -> list[ContentSimpleItem]:
    """
    向服务包添加内容关联（支持批量）。

    输入参数：
        package_id      服务包 id
        req.content_ids 要关联的内容 id 列表
    输出：
        关联成功后，返回当前服务包已关联内容列表
    业务规则：
        - 重复添加同一内容时跳过（幂等）
        - 内容不存在时抛 400
    """
    logger.info(f"add_contents_to_package 入参: package_id={package_id}, req={req}")
    await _get_package_or_404(db, package_id)

    # 查询已关联的 content_id 集合
    existing_rows = (
        await db.execute(
            select(ContentPackage.content_id).where(ContentPackage.package_id == package_id)
        )
    ).scalars().all()
    existing_ids = set(existing_rows)

    for cid in req.content_ids:
        if cid in existing_ids:
            continue
        # 校验内容是否存在
        content = (
            await db.execute(select(Content).where(Content.id == cid, Content.is_deleted.is_(False)))
        ).scalar_one_or_none()
        if not content:
            raise BusinessException(ErrorCode.CONTENT_ID_NOT_FOUND, get_msg("CONTENT_ID_NOT_FOUND"))
        db.add(ContentPackage(content_id=cid, package_id=package_id))
        existing_ids.add(cid)

    await db.commit()

    all_rows = (
        await db.execute(
            select(ContentPackage)
            .where(ContentPackage.package_id == package_id)
            .order_by(ContentPackage.allocated_at.asc())
        )
    ).scalars().all()

    # 收集 genre_id 批量查询
    genre_ids: set[int] = set()
    valid_rows: list[tuple[ContentPackage, Content]] = []
    for row in all_rows:
        c = row.content
        if c and not c.is_deleted:
            valid_rows.append((row, c))
            if c.genre_id:
                genre_ids.add(c.genre_id)

    genre_map: dict[int, str] = {}
    if genre_ids:
        genre_result = await db.execute(select(Genre).where(Genre.id.in_(genre_ids), Genre.is_deleted.is_(False)))
        genre_map = {g.id: g.name for g in genre_result.scalars().all()}

    result: list[ContentSimpleItem] = []
    for row, c in valid_rows:
        result.append(ContentSimpleItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            genre=genre_map.get(c.genre_id) if c.genre_id else None,
            status=c.status,
            has_license=False,
        ))
    return result


async def remove_content_from_package(db: AsyncSession, package_id: int, content_id: int) -> None:
    """
    从服务包移除内容关联。

    输入参数：
        package_id  服务包 id
        content_id  要移除的内容 id
    """
    logger.info(f"remove_content_from_package 入参: package_id={package_id}, content_id={content_id}")
    await _get_package_or_404(db, package_id)

    row = (
        await db.execute(
            select(ContentPackage).where(
                ContentPackage.package_id == package_id,
                ContentPackage.content_id == content_id,
                ContentPackage.is_deleted.is_(False), ContentPackage.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()

    if row:
        await db.delete(row)
        await db.commit()


# ─── 可选内容列表（Add Content to Package 弹框）─────────────────────

async def list_available_contents(
    db: AsyncSession,
    package_id: int,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = None,
    genre_ids: list[int] | None = None,
    custom_tag_ids: list[int] | None = None,
) -> PaginatedResponse[ContentSimpleItem]:
    """
    查询可添加至服务包的内容列表（不包含已关联内容）。

    输入参数：
        package_id      当前服务包 id（用于排除已关联内容）
        page            页码
        page_size       每页条数
        title           内容标题关键字（模糊匹配）
        content_types   内容类型过滤列表（MOVIE/SERIES/SEASON/CHANNEL 等）
        genre_ids       题材 id 列表过滤
        custom_tag_ids  自定义标签 id 列表过滤
    输出：
        PaginatedResponse[ContentSimpleItem]
    """
    logger.info(f"list_available_contents 入参: package_id={package_id}, page={page}, page_size={page_size}, title={title}, content_types={content_types}, genre_ids={genre_ids}, custom_tag_ids={custom_tag_ids}")
    # 已关联至该 package 的内容 id
    from app.internal.cms_biz_package.models.package import ContentCustomTag
    from app.internal.cms_biz_metada.models.basic import CustomTag

    already_linked = (
        await db.execute(
            select(ContentPackage.content_id).where(ContentPackage.package_id == package_id)
        )
    ).scalars().all()
    already_linked_ids = set(already_linked)

    query = select(Content).where(Content.is_deleted.is_(False))

    if already_linked_ids:
        query = query.where(Content.id.notin_(already_linked_ids))
    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if genre_ids:
        query = query.where(Content.genre_id.in_(genre_ids))
    if custom_tag_ids:
        # 筛选包含任一指定自定义标签的内容
        content_ids_with_tags = (
            select(ContentCustomTag.content_id)
            .join(CustomTag, CustomTag.id == ContentCustomTag.custom_tag_id)
            .where(
                ContentCustomTag.custom_tag_id.in_(custom_tag_ids),
                ContentCustomTag.is_deleted.is_(False),
                CustomTag.is_deleted.is_(False),
            )
            .distinct()
        )
        query = query.where(Content.id.in_(content_ids_with_tags))

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    contents = (
        await db.execute(
            query.order_by(Content.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    # 收集 genre_id 批量查询
    content_genre_ids: set[int] = set()
    content_ids: list[int] = []
    for c in contents:
        content_ids.append(c.id)
        if c.genre_id:
            content_genre_ids.add(c.genre_id)

    # 查询题材名称
    genre_map: dict[int, str] = {}
    if content_genre_ids:
        genre_result = await db.execute(select(Genre).where(Genre.id.in_(content_genre_ids), Genre.is_deleted.is_(False)))
        genre_map = {g.id: g.name for g in genre_result.scalars().all()}

    # 查询自定义标签
    content_tags_map: dict[int, list[str]] = {}
    if content_ids:
        tags_result = await db.execute(
            select(ContentCustomTag.content_id, CustomTag.name)
            .join(CustomTag, CustomTag.id == ContentCustomTag.custom_tag_id)
            .where(
                ContentCustomTag.content_id.in_(content_ids),
                ContentCustomTag.is_deleted.is_(False),
                CustomTag.is_deleted.is_(False),
            )
        )
        for content_id, tag_name in tags_result.all():
            if content_id not in content_tags_map:
                content_tags_map[content_id] = []
            content_tags_map[content_id].append(tag_name)

    items = [
        ContentSimpleItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            genre=genre_map.get(c.genre_id) if c.genre_id else None,
            custom_tags=content_tags_map.get(c.id, []),
            status=c.status,
            has_license=False,
        )
        for c in contents
    ]
    logger.info(f"list_available_contents 出参: total={total}, page={page}, items_count={len(items)}")
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )
