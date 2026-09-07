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

from ..models.package import Content, ContentGenre, ContentPackage, Package, PackagePlatform
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
from app.internal.cms_biz_scp.models.trade import License, LicenseContent
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode
from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status, rollback_after_published_edit

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
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS", name=name))


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
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS", name=data.name))

    for platform in data.platforms:
        db.add(PackagePlatform(package_id=package.id, platform=platform))

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS", name=data.name))
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
        raise BusinessException(ErrorCode.PACKAGE_NAME_EXISTS, get_msg("PACKAGE_NAME_EXISTS", name=package.name))
    await db.refresh(package)
    return package


async def delete_package(db: AsyncSession, package_id: int) -> None:
    """
    软删除服务包（is_deleted=True）。

    约束：服务包下存在关联内容时，不允许删除。

    输入参数：
        package_id  服务包 id
    """
    logger.info(f"delete_package 入参: package_id={package_id}")
    package = await _get_package_or_404(db, package_id)

    # 检查是否存在关联内容
    from app.internal.cms_biz_package.models.package import ContentPackage
    has_contents = await db.execute(
        select(ContentPackage.id).where(
            ContentPackage.package_id == package_id,
            ContentPackage.is_deleted.is_(False),
        ).limit(1)
    )
    if has_contents.scalar_one_or_none() is not None:
        raise BusinessException(ErrorCode.PACKAGE_HAS_CONTENTS, get_msg("PACKAGE_HAS_CONTENTS"))

    package.is_deleted = True
    await db.commit()


async def batch_delete_packages(db: AsyncSession, req: BatchDeleteRequest) -> int:
    """
    批量软删除服务包。

    约束：存在关联内容的服务包不会被删除。

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

    from app.internal.cms_biz_package.models.package import ContentPackage
    deleted = 0
    for pkg in packages:
        # 检查是否存在关联内容
        has_contents = await db.execute(
            select(ContentPackage.id).where(
                ContentPackage.package_id == pkg.id,
                ContentPackage.is_deleted.is_(False),
            ).limit(1)
        )
        if has_contents.scalar_one_or_none() is not None:
            logger.warning(f"跳过删除服务包 {pkg.id}（{pkg.name}）：存在关联内容")
            continue
        pkg.is_deleted = True
        deleted += 1
    await db.commit()
    return deleted


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

    # 批量查询 genre 名称（从 content_genre 中间表）
    content_ids_list = [cp.content_id for cp in rows]
    genre_names_map: dict[int, str] = {}
    if content_ids_list:
        from collections import defaultdict
        genre_rows = await db.execute(
            select(ContentGenre.content_id, Genre.name)
            .join(Genre, ContentGenre.genre_id == Genre.id)
            .where(ContentGenre.content_id.in_(content_ids_list), Genre.is_deleted.is_(False))
        )
        temp: dict[int, list[str]] = defaultdict(list)
        for r in genre_rows.all():
            temp[r.content_id].append(r.name)
        genre_names_map = {cid: ", ".join(names) for cid, names in temp.items()}

    # 收集 content_id 用于批量查询许可证关联
    content_ids: list[int] = [cp.content_id for cp in rows]

    # 查询许可证关联（批量）
    licensed_content_ids: set[int] = set()
    if content_ids:
        license_rows = await db.execute(
            select(LicenseContent.content_id)
            .join(License, License.id == LicenseContent.license_id)
            .where(
                LicenseContent.content_id.in_(content_ids),
                LicenseContent.is_deleted.is_(False),
                License.is_deleted.is_(False),
            )
            .distinct()
        )
        licensed_content_ids = set(license_rows.scalars().all())

    # 批量查询 Content 对象获取详细信息
    content_map: dict[int, Content] = {}
    if content_ids:
        content_rows = await db.execute(
            select(Content).where(Content.id.in_(content_ids), Content.is_deleted.is_(False))
        )
        content_map = {c.id: c for c in content_rows.scalars().all()}

    items: list[ContentSimpleItem] = []
    for cp in rows:
        c = content_map.get(cp.content_id)
        if not c:
            continue
        items.append(ContentSimpleItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            genre=genre_names_map.get(c.id),
            status=c.status,
            has_license=c.id in licensed_content_ids,
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
    processed_by: str | None = None,
) -> list[ContentSimpleItem]:
    """
    向服务包添加内容关联（支持批量）。

    输入参数：
        package_id      服务包 id
        req.content_ids 要关联的内容 id 列表
        processed_by   操作人用户名（用于流程记录）
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

    # 追踪本次新增的内容
    new_contents: list[tuple[int, str]] = []  # (content_id, content_type)

    for cid in req.content_ids:
        if cid in existing_ids:
            continue
        # 校验内容是否存在
        content = (
            await db.execute(select(Content).where(Content.id == cid, Content.is_deleted.is_(False)))
        ).scalar_one_or_none()
        if not content:
            raise BusinessException(ErrorCode.CONTENT_ID_NOT_FOUND, get_msg("CONTENT_ID_NOT_FOUND", cid=cid))
        db.add(ContentPackage(content_id=cid, package_id=package_id))
        existing_ids.add(cid)
        new_contents.append((cid, content.content_type))

    await db.commit()

    # 为每个新增的内容记录流程
    if new_contents:
        for cid, ctype in new_contents:
            await complete_process_and_update_status(
                db,
                content_id=cid,
                content_type=ctype,
                process_name="Package",
                processed_by=processed_by,
                info=f"通过服务包(ID={package_id})添加",
            )
        await db.commit()

    # 查询服务包关联的所有内容
    all_rows = (
        await db.execute(
            select(Content)
            .join(ContentPackage, ContentPackage.content_id == Content.id)
            .where(ContentPackage.package_id == package_id, Content.is_deleted.is_(False))
            .order_by(ContentPackage.allocated_at.asc())
        )
    ).scalars().all()

    # 批量查询 genre 名称（从 content_genre 中间表）
    content_ids_list = [c.id for c in all_rows]
    genre_names_map: dict[int, str] = {}
    if content_ids_list:
        from collections import defaultdict
        genre_rows = await db.execute(
            select(ContentGenre.content_id, Genre.name)
            .join(Genre, ContentGenre.genre_id == Genre.id)
            .where(ContentGenre.content_id.in_(content_ids_list), Genre.is_deleted.is_(False))
        )
        temp: dict[int, list[str]] = defaultdict(list)
        for r in genre_rows.all():
            temp[r.content_id].append(r.name)
        genre_names_map = {cid: ", ".join(names) for cid, names in temp.items()}

    result: list[ContentSimpleItem] = []
    for c in all_rows:
        result.append(ContentSimpleItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            genre=genre_names_map.get(c.id),
            status=c.status,
            has_license=False,
        ))
    return result


async def remove_content_from_package(
    db: AsyncSession, package_id: int, content_id: int, processed_by: str | None = None
) -> None:
    """
    从服务包移除内容关联。

    输入参数：
        package_id    服务包 id
        content_id    要移除的内容 id
        processed_by  操作人用户名（用于流程记录）
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

    if not row:
        return

    await db.delete(row)
    await db.commit()

    # 写入一条 info 以"删除"开头的 Package 流程记录，作为 processed_before 的历史分界点，
    # 确保移除后再次添加时 Processed Before 判定为首次处理（红色）
    content = (
        await db.execute(
            select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
        )
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
    查询可添加至服务包的内容列表（所有未删除的内容）。

    输入参数：
        package_id      当前服务包 id
        page            页码
        page_size       每页条数
        title           内容标题关键字（模糊匹配）
        content_types   内容类型过滤列表（MOVIE/SERIES/SEASON/CHANNEL 等）
        genre_ids       题材 id 列表过滤
        custom_tag_ids  自定义标签 id 列表过滤
    输出：
        PaginatedResponse[ContentSimpleItem]

    业务规则：
        - 返回所有未删除的内容（不限状态）
        - 已关联内容也会显示（前端根据已关联列表禁用 + 按钮）
        - 后端 add_contents_to_package 幂等处理，重复绑定自动跳过
    """
    logger.info(f"list_available_contents 入参: package_id={package_id}, page={page}, page_size={page_size}, title={title}, content_types={content_types}, genre_ids={genre_ids}, custom_tag_ids={custom_tag_ids}")
    from app.internal.cms_biz_package.models.package import ContentCustomTag
    from app.internal.cms_biz_metada.models.basic import CustomTag

    query = select(Content).where(
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if genre_ids:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id.in_(genre_ids))
        query = query.where(Content.id.in_(subq))
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

    # 从 content_genre 中间表批量查询 genre 名称
    content_ids: list[int] = [c.id for c in contents]
    genre_rows = await db.execute(
        select(ContentGenre.content_id, Genre.name)
        .join(Genre, ContentGenre.genre_id == Genre.id)
        .where(
            ContentGenre.content_id.in_(content_ids),
            ContentGenre.is_deleted.is_(False),
            Genre.is_deleted.is_(False),
        )
    )
    content_genre_map: dict[int, list[str]] = {}
    for cid, gname in genre_rows.all():
        content_genre_map.setdefault(cid, []).append(gname)

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

    # 查询许可证关联（批量）
    licensed_content_ids: set[int] = set()
    if content_ids:
        license_rows = await db.execute(
            select(LicenseContent.content_id)
            .join(License, License.id == LicenseContent.license_id)
            .where(
                LicenseContent.content_id.in_(content_ids),
                LicenseContent.is_deleted.is_(False),
                License.is_deleted.is_(False),
            )
            .distinct()
        )
        licensed_content_ids = set(license_rows.scalars().all())

    items = [
        ContentSimpleItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            genre=", ".join(content_genre_map.get(c.id, [""])) or None,
            custom_tags=content_tags_map.get(c.id, []),
            status=c.status,
            has_license=c.id in licensed_content_ids,
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


# ─── Package Export（导出）────────────────────────────────────────────

async def export_packages_excel(db: AsyncSession, ids: list[int]) -> bytes:
    """
    导出服务包为 Excel 文件（双 Sheet）。

    Sheet1 "Package"：服务包全部字段
    Sheet2 "Contents"：关联内容列表

    Args:
        db: 数据库会话
        ids: 要导出的服务包 ID 列表

    Returns:
        Excel 文件的二进制数据
    """
    import io
    import openpyxl
    from app.config import app_tz
    from openpyxl.styles import Font, Fill, PatternFill, Alignment
    from app.internal.cms_biz_system.models.dict import DictNode

    # 查询服务包（与页面列表默认排序一致：id 降序）
    packages_result = await db.execute(
        select(Package).where(
            Package.id.in_(ids),
            Package.is_deleted.is_(False),
        ).order_by(Package.id.desc())
    )
    packages = packages_result.scalars().all()

    if not packages:
        raise BusinessException(ErrorCode.PACKAGE_NOT_FOUND, get_msg("PACKAGE_NOT_FOUND"))

    # 批量查询字典映射（Package_Type 和 Platform）
    # 1. 先查根节点
    root_result = await db.execute(
        select(DictNode).where(
            DictNode.is_deleted.is_(False),
            DictNode.code.in_(["Package_Type", "Platform"]),
        )
    )
    root_nodes = root_result.scalars().all()
    root_ids = [n.id for n in root_nodes]
    
    # 2. 再查子节点（子节点的 code 才是实际的类型值/平台编码）
    children_result = await db.execute(
        select(DictNode).where(
            DictNode.is_deleted.is_(False),
            DictNode.parent_id.in_(root_ids),
        )
    )
    children = children_result.scalars().all()
    
    # 3. 按根节点分别构建映射，避免不同字典树子节点 code 重复互相覆盖
    root_code_by_id = {n.id: n.code for n in root_nodes}
    package_type_map: dict[str, str] = {}
    platform_map: dict[str, str] = {}
    for child in children:
        root_code = root_code_by_id.get(child.parent_id)
        if root_code == "Package_Type":
            package_type_map[child.code] = child.name
        elif root_code == "Platform":
            platform_map[child.code] = child.name

    # 批量查询平台
    package_ids = [p.id for p in packages]
    platforms_result = await db.execute(
        select(PackagePlatform).where(
            PackagePlatform.package_id.in_(package_ids),
            PackagePlatform.is_deleted.is_(False),
        ).order_by(PackagePlatform.created_at.asc(), PackagePlatform.platform.asc())
    )
    platforms_map: dict[int, list[str]] = {}
    for pp in platforms_result.scalars().all():
        pkg_id = pp.package_id
        if pkg_id not in platforms_map:
            platforms_map[pkg_id] = []
        platforms_map[pkg_id].append(pp.platform)

    # 批量查询关联内容
    contents_result = await db.execute(
        select(ContentPackage, Content)
        .join(Content, ContentPackage.content_id == Content.id)
        .where(
            ContentPackage.package_id.in_(package_ids),
            ContentPackage.is_deleted.is_(False),
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),
        )
    )

    # 查询结果只能消费一次，先取出全部行复用（ChunkedIteratorResult 无 rewind 方法）
    content_rows = contents_result.all()

    # 收集 content_id 批量加载 genre 名称
    content_ids = [content.id for _cp, content in content_rows]
    genre_rows = await db.execute(
        select(ContentGenre.content_id, Genre.name)
        .join(Genre, ContentGenre.genre_id == Genre.id)
        .where(
            ContentGenre.content_id.in_(content_ids),
            ContentGenre.is_deleted.is_(False),
            Genre.is_deleted.is_(False),
        )
    )
    content_genre_map: dict[int, list[str]] = {}
    for cid, gname in genre_rows.all():
        content_genre_map.setdefault(cid, []).append(gname)

    # 组织内容数据
    contents_by_package: dict[int, list[tuple]] = {}
    for cp, content in content_rows:
        pkg_id = cp.package_id
        if pkg_id not in contents_by_package:
            contents_by_package[pkg_id] = []
        genre_names = content_genre_map.get(content.id, [])
        genre_display = ", ".join(genre_names) if genre_names else "—"
        contents_by_package[pkg_id].append((
            content.title,
            content.content_type,
            genre_display,
            content.status,
        ))

    # 创建 Excel
    wb = openpyxl.Workbook()

    # Sheet1: Package
    ws1 = wb.active
    ws1.title = "Package"

    headers1 = ["Package Name", "Package Type", "Platforms", "Description", "Ingest Status", "Created At"]
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col, header in enumerate(headers1, 1):
        cell = ws1.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row_idx, pkg in enumerate(packages, 2):
        # 转换 Platform 编码为名称（空值与页面一致显示 —）
        platform_codes = platforms_map.get(pkg.id, [])
        platform_names = [platform_map.get(code, code) for code in platform_codes]
        platforms_str = ", ".join(platform_names) if platform_names else "—"

        # 转换 Package Type 编码为名称
        package_type_name = package_type_map.get(pkg.package_type, pkg.package_type) if pkg.package_type else "—"

        created_at_str = pkg.created_at.astimezone(app_tz).strftime("%Y-%m-%d %H:%M:%S") if pkg.created_at else ""
        row_data = [
            pkg.name,
            package_type_name,
            platforms_str,
            pkg.description or "—",
            pkg.ingest_status,
            created_at_str,
        ]
        for col_idx, value in enumerate(row_data, 1):
            ws1.cell(row=row_idx, column=col_idx, value=value)

    # 设置列宽
    for col, width in enumerate([30, 20, 30, 40, 20, 25], 1):
        ws1.column_dimensions[ws1.cell(row=1, column=col).column_letter].width = width

    # Sheet2: Contents
    ws2 = wb.create_sheet(title="Contents")

    headers2 = ["Package Name", "Content Name", "Content Type", "Genre", "Ingest Status"]
    for col, header in enumerate(headers2, 1):
        cell = ws2.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    row_idx = 2
    for pkg in packages:
        pkg_contents = contents_by_package.get(pkg.id, [])
        if not pkg_contents:
            # 即使没有内容也显示一行
            ws2.cell(row=row_idx, column=1, value=pkg.name)
            row_idx += 1
        else:
            for content_title, content_type, genre, status in pkg_contents:
                ws2.cell(row=row_idx, column=1, value=pkg.name)
                ws2.cell(row=row_idx, column=2, value=content_title)
                ws2.cell(row=row_idx, column=3, value=content_type)
                ws2.cell(row=row_idx, column=4, value=genre)
                ws2.cell(row=row_idx, column=5, value=status)
                row_idx += 1

    # 设置列宽
    for col, width in enumerate([30, 40, 20, 20, 20], 1):
        ws2.column_dimensions[ws2.cell(row=1, column=col).column_letter].width = width

    # 保存到内存
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return buf.getvalue()


# ─── Package Import（导入）────────────────────────────────────────────

from app.internal.cms_biz_package.schemas.package import PackageImportResult, PackageImportError

async def import_package_contents_excel(
    db: AsyncSession,
    file_content: bytes,
    processed_by: str | None = None,
) -> tuple[PackageImportResult, list[dict], list[dict]]:
    """
    导入服务包内容关联关系。

    导入字段：
        - Package Name: 服务包名称
        - Content Name: 内容名称
        - Operation Mode: Add（添加关联）/ DEL（取消关联）

    严格校验：
        1. Package Name 是否存在且未删除
        2. Content Name 是否存在且未删除/未废弃
        3. Operation Mode 必须是 Add 或 DEL
        4. Add 模式：检查关联是否已存在
        5. DEL 模式：检查关联是否存在

    Args:
        db: 数据库会话
        file_content: Excel 文件二进制数据

    Returns:
        (导入结果统计, 本次新增关联明细, 本次移除关联明细)
    """
    import io
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(file_content), read_only=True)
    ws = wb.active

    if not ws:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("INVALID_EXCEL_FORMAT"))

    # 校验表头是否为服务包导入模板
    header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
    if not header_row:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("INVALID_EXCEL_FORMAT"))

    required_headers = ["Package Name", "Content Name", "Operation Mode"]
    header_list = [str(h).strip() if h else "" for h in header_row]
    for h in required_headers:
        if h not in header_list:
            raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("INVALID_EXCEL_FORMAT"))

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    result = PackageImportResult()

    # 追踪本次变更明细，供 API 层按内容维度写操作日志
    added: list[dict] = []
    removed: list[dict] = []

    valid_modes = {"ADD", "DEL"}

    for idx, row in enumerate(rows, start=2):
        # 跳过完全空行（没有 Package Name 的行视为空行）
        if not row or not row[0]:
            continue

        package_name = str(row[0]).strip()
        content_name = str(row[1]).strip() if row[1] else ""

        # 有 Package Name 但没有 Content Name，视为无效行跳过
        if not content_name:
            continue

        result.total += 1

        operation_mode_raw = str(row[2]).strip() if row[2] else ""

        # 校验 Operation Mode 是否为空
        if not operation_mode_raw:
            result.errors.append(PackageImportError(
                row=idx,
                package_name=package_name,
                content_name=content_name,
                error_message=get_msg("PACKAGE_IMPORT_MODE_REQUIRED"),
            ))
            result.skipped += 1
            continue

        operation_mode = operation_mode_raw.upper()

        # 校验 Operation Mode 合法性
        if operation_mode not in valid_modes:
            result.errors.append(PackageImportError(
                row=idx,
                package_name=package_name,
                content_name=content_name,
                error_message=get_msg("PACKAGE_IMPORT_MODE_INVALID", mode=operation_mode_raw),
            ))
            result.skipped += 1
            continue

        # 校验 Package Name 是否存在
        pkg_result = await db.execute(
            select(Package).where(
                Package.name == package_name,
                Package.is_deleted.is_(False),
            )
        )
        package = pkg_result.scalars().first()

        if not package:
            result.errors.append(PackageImportError(
                row=idx,
                package_name=package_name,
                content_name=content_name,
                error_message=get_msg("PACKAGE_IMPORT_PACKAGE_NOT_FOUND", name=package_name),
            ))
            result.skipped += 1
            continue

        # 校验 Content Name 是否存在
        content_result = await db.execute(
            select(Content).where(
                Content.title == content_name,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
        content = content_result.scalars().first()

        if not content:
            result.errors.append(PackageImportError(
                row=idx,
                package_name=package_name,
                content_name=content_name,
                error_message=get_msg("PACKAGE_IMPORT_CONTENT_NOT_FOUND", name=content_name),
            ))
            result.skipped += 1
            continue

        # 查询关联（不过滤 is_deleted：唯一约束 uq_content_package 不含 is_deleted，
        # 同一 content+package 至多一行，据此复活历史软删记录，避免 ADD 时唯一约束冲突）
        existing_result = await db.execute(
            select(ContentPackage).where(
                ContentPackage.package_id == package.id,
                ContentPackage.content_id == content.id,
            )
        )
        existing = existing_result.scalar_one_or_none()

        if operation_mode == "ADD":
            if existing and not existing.is_deleted:
                # 关联已存在，跳过
                result.skipped += 1
                continue

            if existing:
                # 复活历史软删记录
                existing.is_deleted = False
                existing.is_discarded = False
            else:
                # 创建新关联
                db.add(ContentPackage(
                    package_id=package.id,
                    content_id=content.id,
                ))
            result.created += 1
            added.append({
                "package_id": package.id,
                "package_name": package.name,
                "content_id": content.id,
                "content_name": content.title,
                "content_type": content.content_type,
            })

        elif operation_mode == "DEL":
            if not existing or existing.is_deleted:
                # 关联不存在，跳过
                result.skipped += 1
                continue

            # 物理删除关联（与正常移除路径一致，避免软删残留导致唯一约束冲突）
            await db.delete(existing)
            result.deleted += 1
            removed.append({
                "package_id": package.id,
                "package_name": package.name,
                "content_id": content.id,
                "content_name": content.title,
                "content_type": content.content_type,
            })

    # 提交关联变更
    await db.commit()

    # 补写流程记录并回滚已发布状态（与正常添加/移除路径行为一致）
    for item in added:
        await complete_process_and_update_status(
            db,
            content_id=item["content_id"],
            content_type=item["content_type"],
            process_name="Package",
            processed_by=processed_by,
            info=f"通过服务包(ID={item['package_id']})添加",
        )
        await rollback_after_published_edit(
            db, item["content_id"], item["content_type"],
            processed_by or "system", "关联服务包",
        )
    for item in removed:
        # info 以"删除"开头，作为 Processed Before 分界标记
        await complete_process_and_update_status(
            db,
            content_id=item["content_id"],
            content_type=item["content_type"],
            process_name="Package",
            processed_by=processed_by,
            info=f"删除服务包关联: package_id={item['package_id']}",
        )
        await rollback_after_published_edit(
            db, item["content_id"], item["content_type"],
            processed_by or "system", "取消服务包关联",
        )
    if added or removed:
        await db.commit()

    return result, added, removed
