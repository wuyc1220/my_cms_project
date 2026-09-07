"""
内容采编模块 - 数据访问层
封装 Content / Picture 等表的 SQLAlchemy 查询操作
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_package.models.package import Content, ContentGenre, ContentPackage, Package
from app.internal.cms_biz_scp.models.trade import License, LicenseContent, Provider
from app.internal.cms_biz_package.models.package import PhysicalChannel


# ═══════════════════════════════════════════════════════════
# Content
# ═══════════════════════════════════════════════════════════

async def get_content_by_id(db: AsyncSession, content_id: int) -> Content | None:
    """按ID查询内容"""
    return (await db.execute(
        select(Content).where(Content.id == content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )).scalar_one_or_none()


async def list_contents_query(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_type: str | None = None,
    status: str | None = None,
    genre_id: int | None = None,
    is_archived: bool | None = None,
    is_discarded: bool | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[Content], int]:
    query = select(Content).where(Content.is_deleted.is_(False))
    if is_discarded is not None:
        query = query.where(Content.is_discarded.is_(is_discarded))
    else:
        query = query.where(Content.is_discarded.is_(False))
    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_type:
        query = query.where(Content.content_type == content_type)
    if status:
        query = query.where(Content.status == status)
    if genre_id is not None:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id == genre_id, ContentGenre.is_deleted.is_(False))
        query = query.where(Content.id.in_(subq))
    if is_archived is not None:
        query = query.where(Content.is_archived == is_archived)

    if sort_by and sort_order:
        sort_column = getattr(Content, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(Content.id.desc())
    else:
        query = query.order_by(Content.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_contents_by_ids(db: AsyncSession, ids: list[int]) -> list[Content]:
    """按ID列表查询内容"""
    return (await db.execute(
        select(Content).where(Content.id.in_(ids), Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )).scalars().all()


async def get_child_contents(db: AsyncSession, parent_id: int) -> list[Content]:
    """查询子内容"""
    return (await db.execute(
        select(Content).where(Content.parent_id == parent_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )).scalars().all()


async def count_child_contents(db: AsyncSession, parent_id: int) -> int:
    """统计子内容数量"""
    return (await db.execute(
        select(func.count(Content.id)).where(Content.parent_id == parent_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )).scalar_one()


async def add_content(db: AsyncSession, content: Content) -> None:
    """新增内容"""
    db.add(content)


# ── Content 许可证关联查询 ──────────────────────────────────

async def get_content_license_ids(db: AsyncSession, content_id: int) -> list[int]:
    """获取内容关联的许可证ID列表"""
    return list((await db.execute(
        select(LicenseContent.license_id).where(
            LicenseContent.content_id == content_id,
            LicenseContent.is_deleted.is_(False),
        )
    )).scalars().all())


async def get_content_license_info(db: AsyncSession, content_id: int) -> list[tuple[int, str | None, str | None]]:
    """获取内容关联的许可证信息 (license_id, license_name, provider_name)"""
    lc_rows = (await db.execute(
        select(LicenseContent.license_id).where(
            LicenseContent.content_id == content_id,
            LicenseContent.is_deleted.is_(False),
        )
    )).scalars().all()
    if not lc_rows:
        return []

    result = []
    licenses = (await db.execute(
        select(License).where(License.id.in_(lc_rows), License.is_deleted.is_(False))
    )).scalars().all()
    for lic in licenses:
        provider_name = None
        if lic.contract and lic.contract.provider:
            provider_name = lic.contract.provider.name
        result.append((lic.id, lic.name, provider_name))
    return result


async def count_contents_without_license(db: AsyncSession) -> int:
    """统计无许可证的内容数量"""
    sub_lc = select(LicenseContent.content_id).where(LicenseContent.is_deleted.is_(False))
    return (await db.execute(
        select(func.count(Content.id)).where(
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),
            Content.id.notin_(sub_lc),
        )
    )).scalar_one()


# ── Genre 辅助 ──────────────────────────────────────────────

async def get_genre_name(db: AsyncSession, genre_id: int | None) -> str | None:
    if genre_id is None:
        return None
    return (await db.execute(select(Genre.name).where(Genre.id == genre_id, Genre.is_deleted.is_(False)))).scalar_one_or_none()


# ── Content 包关联 ──────────────────────────────────────────

async def get_content_packages(db: AsyncSession, content_id: int) -> list[ContentPackage]:
    """获取内容关联的包"""
    return (await db.execute(
        select(ContentPackage).where(
            ContentPackage.content_id == content_id,
            ContentPackage.is_deleted.is_(False), ContentPackage.is_discarded.is_(False)
        )
    )).scalars().all()


# ═══════════════════════════════════════════════════════════
# PhysicalChannel / LogicalChannel / Schedule
# ═══════════════════════════════════════════════════════════

async def get_physical_channel_by_id(db: AsyncSession, channel_id: int) -> PhysicalChannel | None:
    return (await db.execute(
        select(PhysicalChannel).where(PhysicalChannel.id == channel_id, PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False))
    )).scalar_one_or_none()


async def list_physical_channels_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    name: str | None = None, sort_by: str | None = None, sort_order: str | None = None,
) -> tuple[list[PhysicalChannel], int]:
    query = select(PhysicalChannel).where(PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False))
    if name:
        query = query.where(PhysicalChannel.name.ilike(f"%{name}%"))

    if sort_by and sort_order:
        sort_column = getattr(PhysicalChannel, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(PhysicalChannel.id.desc())
    else:
        query = query.order_by(PhysicalChannel.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_physical_channels_by_ids(db: AsyncSession, ids: list[int]) -> list[PhysicalChannel]:
    return (await db.execute(
        select(PhysicalChannel).where(PhysicalChannel.id.in_(ids), PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False))
    )).scalars().all()


async def add_physical_channel(db: AsyncSession, ch: PhysicalChannel) -> None:
    db.add(ch)


# TODO: LogicalChannel / Schedule / ChannelCategory 模型尚未创建，以下函数暂注释
# async def get_logical_channel_by_id(db: AsyncSession, channel_id: int) -> LogicalChannel | None:
#     ...

# async def list_logical_channels_query(...) -> tuple[list[LogicalChannel], int]:
#     ...

# async def get_schedule_by_id(db: AsyncSession, schedule_id: int) -> Schedule | None:
#     ...

# async def list_schedules_query(...) -> tuple[list[Schedule], int]:
#     ...

# async def get_channel_categories_by_channel_id(db: AsyncSession, channel_id: int) -> list[ChannelCategory]:
#     ...
