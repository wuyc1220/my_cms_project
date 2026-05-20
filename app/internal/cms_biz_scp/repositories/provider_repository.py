"""
供应商（Provider）数据访问层
封装所有对 Provider 表的 SQLAlchemy 查询操作
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_scp.models.trade import Contract, Provider
from app.internal.cms_biz_system.models.user import User


# ── Provider 查询 ──────────────────────────────────────────

async def get_provider_by_id(db: AsyncSession, provider_id: int) -> Provider | None:
    """按ID查询供应商"""
    result = await db.execute(
        select(Provider).where(Provider.id == provider_id, Provider.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


async def get_provider_by_name(db: AsyncSession, name: str, exclude_id: int | None = None) -> Provider | None:
    """按名称查询供应商（可排除指定ID）"""
    query = select(Provider).where(Provider.name == name, Provider.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(Provider.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none()


async def list_providers_query(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    country: str | None = None,
    notes: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[Provider], int]:
    """分页查询供应商列表，返回 (providers, total)"""
    query = select(Provider).where(Provider.is_deleted.is_(False))
    if name:
        query = query.where(Provider.name.ilike(f"%{name}%"))
    if country:
        query = query.where(Provider.country.ilike(f"%{country}%"))
    if notes:
        query = query.where(Provider.notes.ilike(f"%{notes}%"))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Provider, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(Provider.id.desc())
    else:
        query = query.order_by(Provider.id.desc())

    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_providers_by_ids(db: AsyncSession, ids: list[int]) -> list[Provider]:
    """按ID列表查询供应商"""
    result = await db.execute(
        select(Provider).where(Provider.id.in_(ids), Provider.is_deleted.is_(False))
    )
    return result.scalars().all()


async def get_all_providers_simple(db: AsyncSession) -> list[Provider]:
    """获取所有供应商简要列表（用于下拉）"""
    result = await db.execute(
        select(Provider).where(Provider.is_deleted.is_(False)).order_by(Provider.name.asc())
    )
    return result.scalars().all()


async def count_active_providers(db: AsyncSession) -> int:
    """统计未删除的供应商数量"""
    return (await db.execute(
        select(func.count()).select_from(Provider).where(Provider.is_deleted.is_(False))
    )).scalar_one()


# ── Provider 关联查询 ──────────────────────────────────────

async def count_provider_contracts(db: AsyncSession, provider_id: int) -> int:
    """统计供应商关联合同数量"""
    return (await db.execute(
        select(func.count(Contract.id)).where(
            Contract.provider_id == provider_id,
            Contract.is_deleted.is_(False),
        )
    )).scalar_one()


async def has_active_contracts(db: AsyncSession, provider_id: int) -> bool:
    """检查供应商是否有未删除的合同"""
    return (await db.execute(
        select(Contract.id).where(
            Contract.provider_id == provider_id,
            Contract.is_deleted.is_(False),
        ).limit(1)
    )).scalar_one_or_none() is not None


async def get_provider_contracts(db: AsyncSession, provider_id: int) -> list[Contract]:
    """查询供应商下的合同列表"""
    result = await db.execute(
        select(Contract).where(
            Contract.provider_id == provider_id,
            Contract.is_deleted.is_(False),
        ).order_by(Contract.id.desc())
    )
    return result.scalars().all()


# ── Provider 写入 ──────────────────────────────────────────

async def add_provider(db: AsyncSession, provider: Provider) -> None:
    """新增供应商"""
    db.add(provider)


# ── 辅助 ────────────────────────────────────────────────────

async def get_user_display_name(db: AsyncSession, user_id: int | None) -> str | None:
    """查询用户显示名称"""
    if user_id is None:
        return None
    user = (await db.execute(select(User).where(User.id == user_id, User.is_deleted.is_(False)))).scalar_one_or_none()
    if not user:
        return None
    return user.display_name or user.username
