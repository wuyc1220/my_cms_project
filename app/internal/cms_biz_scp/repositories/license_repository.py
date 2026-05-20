"""
许可证（License）数据访问层
封装所有对 License / LicensePlatform / LicenseContent 表的 SQLAlchemy 查询操作
"""
from datetime import date as date_type

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_package.models.package import Content
from app.internal.cms_biz_scp.models.trade import (
    License,
    LicenseContent,
    LicensePlatform,
)


# ── License 查询 ───────────────────────────────────────────

async def get_license_by_id(db: AsyncSession, license_id: int) -> License | None:
    """按ID查询许可证"""
    result = await db.execute(
        select(License).where(License.id == license_id, License.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


async def get_license_by_name(db: AsyncSession, name: str, exclude_id: int | None = None) -> License | None:
    """按名称查询许可证（可排除指定ID）"""
    query = select(License).where(License.name == name, License.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(License.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none()


async def list_licenses_query(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    service_types: list[str] | None = None,
    platforms: list[str] | None = None,
    statuses: list[str] | None = None,
    start_date_from: str | None = None,
    start_date_to: str | None = None,
    end_date_from: str | None = None,
    end_date_to: str | None = None,
    contract_id: int | None = None,
    without_content: bool = False,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[License], int]:
    """分页查询许可证列表，返回 (licenses, total)"""
    query = select(License).where(License.is_deleted.is_(False))

    if name:
        query = query.where(License.name.ilike(f"%{name}%"))
    if service_types:
        query = query.where(License.service_type.in_(service_types))
    if statuses:
        query = query.where(License.status.in_(statuses))
    if platforms:
        sub = select(LicensePlatform.license_id).where(
            LicensePlatform.platform.in_(platforms)
        )
        query = query.where(License.id.in_(sub))
    if start_date_from:
        query = query.where(License.start_date >= date_type.fromisoformat(start_date_from))
    if start_date_to:
        query = query.where(License.start_date <= date_type.fromisoformat(start_date_to))
    if end_date_from:
        query = query.where(License.end_date >= date_type.fromisoformat(end_date_from))
    if end_date_to:
        query = query.where(License.end_date <= date_type.fromisoformat(end_date_to))
    if contract_id is not None:
        query = query.where(License.contract_id == contract_id)
    if without_content:
        sub_content = select(LicenseContent.license_id).where(LicenseContent.is_deleted.is_(False))
        query = query.where(License.id.notin_(sub_content))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(License, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, License.id.desc())
        else:
            query = query.order_by(License.id.desc())
    else:
        query = query.order_by(License.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_licenses_by_ids(db: AsyncSession, ids: list[int]) -> list[License]:
    """按ID列表查询许可证"""
    result = await db.execute(
        select(License).where(License.id.in_(ids), License.is_deleted.is_(False))
    )
    return result.scalars().all()


async def get_contract_licenses(db: AsyncSession, contract_id: int) -> list[License]:
    """获取合同下的许可证列表"""
    result = await db.execute(
        select(License).where(
            License.contract_id == contract_id,
            License.is_deleted.is_(False),
        ).order_by(License.id.desc())
    )
    return result.scalars().all()


# ── License 写入 ───────────────────────────────────────────

async def add_license(db: AsyncSession, lic: License) -> None:
    """新增许可证"""
    db.add(lic)


# ── LicensePlatform ────────────────────────────────────────

async def get_license_platforms(db: AsyncSession, license_id: int) -> list[LicensePlatform]:
    """查询许可证平台关联"""
    result = await db.execute(
        select(LicensePlatform).where(LicensePlatform.license_id == license_id)
    )
    return result.scalars().all()


async def replace_license_platforms(
    db: AsyncSession, license_id: int, platforms: list[LicensePlatform]
) -> None:
    """替换许可证平台关联（删除旧的，添加新的）"""
    old = await get_license_platforms(db, license_id)
    for old_p in old:
        await db.delete(old_p)
    await db.flush()
    for p in platforms:
        db.add(p)


# ── LicenseContent ─────────────────────────────────────────

async def count_license_contents(db: AsyncSession, license_id: int) -> int:
    """统计许可证关联内容数量"""
    return (await db.execute(
        select(func.count(LicenseContent.id)).where(
            LicenseContent.license_id == license_id,
            LicenseContent.is_deleted.is_(False),
        )
    )).scalar_one()


async def get_license_content_ids(db: AsyncSession, license_id: int) -> set[int]:
    """获取许可证已关联的内容ID集合"""
    rows = (await db.execute(
        select(LicenseContent.content_id).where(
            LicenseContent.license_id == license_id,
            LicenseContent.is_deleted.is_(False),
        )
    )).scalars().all()
    return set(rows)


async def get_license_contents(db: AsyncSession, license_id: int) -> list[LicenseContent]:
    """获取许可证关联内容记录"""
    result = await db.execute(
        select(LicenseContent).where(
            LicenseContent.license_id == license_id,
            LicenseContent.is_deleted.is_(False),
        )
        .order_by(LicenseContent.created_at.desc())
    )
    return result.scalars().all()


async def add_license_content(db: AsyncSession, lc: LicenseContent) -> None:
    """新增许可证内容关联"""
    db.add(lc)


async def get_license_content_by_ids(
    db: AsyncSession, license_id: int, content_id: int
) -> LicenseContent | None:
    """查询指定许可证-内容关联"""
    return (await db.execute(
        select(LicenseContent).where(
            LicenseContent.license_id == license_id,
            LicenseContent.content_id == content_id,
            LicenseContent.is_deleted.is_(False),
        )
    )).scalar_one_or_none()


async def delete_license_content(db: AsyncSession, lc: LicenseContent) -> None:
    """软删除许可证内容关联"""
    lc.is_deleted = True


# ── Content 辅助查询 ────────────────────────────────────────

async def get_content_by_id(db: AsyncSession, content_id: int) -> Content | None:
    """按ID查询内容"""
    return (await db.execute(
        select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
    )).scalar_one_or_none()


async def list_available_contents_query(
    db: AsyncSession,
    *,
    already_linked: set[int],
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
    without_license: bool = False,
) -> tuple[list[Content], int]:
    """查询可添加至许可证的内容列表（排除已关联）"""
    query = select(Content).where(Content.is_deleted.is_(False))
    if already_linked:
        query = query.where(Content.id.notin_(already_linked))
    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if ingest_statuses:
        query = query.where(Content.status.in_(ingest_statuses))
    if without_license:
        sub_lc = select(LicenseContent.content_id).where(LicenseContent.is_deleted.is_(False))
        query = query.where(Content.id.notin_(sub_lc))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(
        query.order_by(Content.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return result.scalars().all(), total


async def count_contents_without_license(db: AsyncSession) -> int:
    """统计未关联任何许可证的内容数量"""
    sub_lc = select(LicenseContent.content_id).where(LicenseContent.is_deleted.is_(False))
    return (await db.execute(
        select(func.count(Content.id)).where(
            Content.is_deleted.is_(False),
            Content.id.notin_(sub_lc),
        )
    )).scalar_one()


# ── Genre 辅助查询 ──────────────────────────────────────────

async def get_genre_name(db: AsyncSession, genre_id: int | None) -> str | None:
    """查询题材名称"""
    if genre_id is None:
        return None
    genre = (await db.execute(select(Genre).where(Genre.id == genre_id, Genre.is_deleted.is_(False)))).scalar_one_or_none()
    return genre.name if genre else None


# ── 许可证名称查询（用于内容关联显示）──────────────────────

async def get_license_names_by_content_id(db: AsyncSession, content_id: int) -> list[str]:
    """获取内容关联的许可证名称列表"""
    lic_rows = (await db.execute(
        select(LicenseContent.license_id).where(
            LicenseContent.content_id == content_id,
            LicenseContent.is_deleted.is_(False),
        )
    )).scalars().all()
    if not lic_rows:
        return []
    return list((await db.execute(
        select(License.name).where(
            License.id.in_(lic_rows),
            License.is_deleted.is_(False),
        )
    )).scalars().all())
