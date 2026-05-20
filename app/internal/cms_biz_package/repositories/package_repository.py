"""
内容打包模块 - 数据访问层
封装 Package / PackagePlatform / ContentPackage 的 SQLAlchemy 查询操作
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_package.models.package import Content, ContentPackage, Package, PackagePlatform


# ═══════════════════════════════════════════════════════════
# Package
# ═══════════════════════════════════════════════════════════

async def get_package_by_id(db: AsyncSession, package_id: int) -> Package | None:
    return (await db.execute(
        select(Package).where(Package.id == package_id, Package.is_deleted.is_(False))
    )).scalar_one_or_none()


async def get_package_by_name(db: AsyncSession, name: str, exclude_id: int | None = None) -> Package | None:
    query = select(Package).where(Package.name == name, Package.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(Package.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none()


async def list_packages_query(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    package_type: str | None = None,
    platforms: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
    description: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[Package], int]:
    query = select(Package).where(Package.is_deleted.is_(False))
    if name:
        query = query.where(Package.name.ilike(f"%{name}%"))
    if package_type:
        query = query.where(Package.package_type == package_type)
    if platforms:
        sub = select(PackagePlatform.package_id).where(PackagePlatform.platform.in_(platforms))
        query = query.where(Package.id.in_(sub))
    if ingest_statuses:
        query = query.where(Package.ingest_status.in_(ingest_statuses))
    if description:
        query = query.where(Package.description.ilike(f"%{description}%"))

    if sort_by and sort_order:
        sort_column = getattr(Package, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(Package.id.desc())
    else:
        query = query.order_by(Package.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_packages_by_ids(db: AsyncSession, ids: list[int]) -> list[Package]:
    return (await db.execute(
        select(Package).where(Package.id.in_(ids), Package.is_deleted.is_(False))
    )).scalars().all()


async def add_package(db: AsyncSession, pkg: Package) -> None:
    db.add(pkg)


# ═══════════════════════════════════════════════════════════
# PackagePlatform
# ═══════════════════════════════════════════════════════════

async def get_package_platforms(db: AsyncSession, package_id: int) -> list[PackagePlatform]:
    return (await db.execute(
        select(PackagePlatform).where(PackagePlatform.package_id == package_id)
    )).scalars().all()


async def replace_package_platforms(
    db: AsyncSession, package_id: int, platforms: list[PackagePlatform]
) -> None:
    old = await get_package_platforms(db, package_id)
    for old_p in old:
        await db.delete(old_p)
    await db.flush()
    for p in platforms:
        db.add(p)


# ═══════════════════════════════════════════════════════════
# ContentPackage
# ═══════════════════════════════════════════════════════════

async def get_package_content_ids(db: AsyncSession, package_id: int) -> set[int]:
    rows = (await db.execute(
        select(ContentPackage.content_id).where(ContentPackage.package_id == package_id)
    )).scalars().all()
    return set(rows)


async def get_package_contents(db: AsyncSession, package_id: int) -> list[ContentPackage]:
    return (await db.execute(
        select(ContentPackage).where(
            ContentPackage.package_id == package_id,
            ContentPackage.is_deleted.is_(False), ContentPackage.is_discarded.is_(False)
        ).order_by(ContentPackage.created_at.desc())
    )).scalars().all()


async def add_content_package(db: AsyncSession, cp: ContentPackage) -> None:
    db.add(cp)


async def get_content_package_by_ids(
    db: AsyncSession, package_id: int, content_id: int
) -> ContentPackage | None:
    return (await db.execute(
        select(ContentPackage).where(
            ContentPackage.package_id == package_id,
            ContentPackage.content_id == content_id,
            ContentPackage.is_deleted.is_(False), ContentPackage.is_discarded.is_(False),
        )
    )).scalar_one_or_none()


async def delete_content_package(db: AsyncSession, cp: ContentPackage) -> None:
    await db.delete(cp)


# ── Content 辅助查询 ────────────────────────────────────────

async def get_content_by_id(db: AsyncSession, content_id: int) -> Content | None:
    return (await db.execute(
        select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
    )).scalar_one_or_none()


async def list_available_contents_for_package(
    db: AsyncSession,
    *,
    already_linked: set[int],
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = None,
) -> tuple[list[Content], int]:
    query = select(Content).where(Content.is_deleted.is_(False))
    if already_linked:
        query = query.where(Content.id.notin_(already_linked))
    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.order_by(Content.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total
