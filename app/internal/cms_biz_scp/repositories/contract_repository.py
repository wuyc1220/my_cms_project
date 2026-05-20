"""
合同（Contract）数据访问层
封装所有对 Contract / ContractPlatform / ContractAttachment 表的 SQLAlchemy 查询操作
"""
from datetime import date as date_type

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_scp.models.trade import (
    Contract,
    ContractAttachment,
    ContractPlatform,
    License,
)


# ── Contract 查询 ──────────────────────────────────────────

async def get_contract_by_id(db: AsyncSession, contract_id: int) -> Contract | None:
    """按ID查询合同"""
    result = await db.execute(
        select(Contract).where(Contract.id == contract_id, Contract.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


async def get_contract_by_name(db: AsyncSession, name: str, exclude_id: int | None = None) -> Contract | None:
    """按名称查询合同（可排除指定ID）"""
    query = select(Contract).where(Contract.name == name, Contract.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(Contract.id != exclude_id)
    return (await db.execute(query)).scalar_one_or_none()


async def list_contracts_query(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    provider_id: int | None = None,
    platforms: list[str] | None = None,
    start_date_from: str | None = None,
    start_date_to: str | None = None,
    end_date_from: str | None = None,
    end_date_to: str | None = None,
    without_license: bool = False,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[Contract], int]:
    """分页查询合同列表，返回 (contracts, total)"""
    query = select(Contract).where(Contract.is_deleted.is_(False))

    if name:
        query = query.where(Contract.name.ilike(f"%{name}%"))
    if provider_id is not None:
        query = query.where(Contract.provider_id == provider_id)
    if platforms:
        sub = select(ContractPlatform.contract_id).where(
            ContractPlatform.platform.in_(platforms)
        )
        query = query.where(Contract.id.in_(sub))
    if start_date_from:
        query = query.where(Contract.start_date >= date_type.fromisoformat(start_date_from))
    if start_date_to:
        query = query.where(Contract.start_date <= date_type.fromisoformat(start_date_to))
    if end_date_from:
        query = query.where(Contract.end_date >= date_type.fromisoformat(end_date_from))
    if end_date_to:
        query = query.where(Contract.end_date <= date_type.fromisoformat(end_date_to))
    if without_license:
        sub_license = select(License.contract_id).where(License.is_deleted.is_(False))
        query = query.where(Contract.id.notin_(sub_license))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Contract, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Contract.id.desc())
        else:
            query = query.order_by(Contract.id.desc())
    else:
        query = query.order_by(Contract.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_contracts_by_ids(db: AsyncSession, ids: list[int]) -> list[Contract]:
    """按ID列表查询合同"""
    result = await db.execute(
        select(Contract).where(Contract.id.in_(ids), Contract.is_deleted.is_(False))
    )
    return result.scalars().all()


async def get_all_contracts_simple(db: AsyncSession) -> list[Contract]:
    """获取所有合同简要列表（用于下拉）"""
    result = await db.execute(
        select(Contract).where(Contract.is_deleted.is_(False)).order_by(Contract.name.asc())
    )
    return result.scalars().all()


async def count_contracts_without_license(db: AsyncSession) -> int:
    """统计无关联许可证的合同数量"""
    sub_license = select(License.contract_id).where(License.is_deleted.is_(False))
    return (await db.execute(
        select(func.count(Contract.id)).where(
            Contract.is_deleted.is_(False),
            Contract.id.notin_(sub_license),
        )
    )).scalar_one()


# ── Contract 写入 ──────────────────────────────────────────

async def add_contract(db: AsyncSession, contract: Contract) -> None:
    """新增合同"""
    db.add(contract)


# ── ContractPlatform ───────────────────────────────────────

async def get_contract_platforms(db: AsyncSession, contract_id: int) -> list[ContractPlatform]:
    """查询合同平台关联"""
    result = await db.execute(
        select(ContractPlatform).where(ContractPlatform.contract_id == contract_id)
    )
    return result.scalars().all()


async def replace_contract_platforms(
    db: AsyncSession, contract_id: int, platforms: list[ContractPlatform]
) -> None:
    """替换合同平台关联（删除旧的，添加新的）"""
    old = await get_contract_platforms(db, contract_id)
    for old_p in old:
        await db.delete(old_p)
    await db.flush()
    for p in platforms:
        db.add(p)


# ── License 关联查询 ───────────────────────────────────────

async def has_active_licenses(db: AsyncSession, contract_id: int) -> bool:
    """检查合同是否有未删除的许可证"""
    return (await db.execute(
        select(License.id).where(
            License.contract_id == contract_id,
            License.is_deleted.is_(False),
        ).limit(1)
    )).scalar_one_or_none() is not None


# ── ContractAttachment ─────────────────────────────────────

async def get_contract_attachments(db: AsyncSession, contract_id: int) -> list[ContractAttachment]:
    """查询合同附件列表"""
    result = await db.execute(
        select(ContractAttachment)
        .where(ContractAttachment.contract_id == contract_id)
        .order_by(ContractAttachment.created_at.desc())
    )
    return result.scalars().all()


async def get_attachment_by_id(db: AsyncSession, contract_id: int, attachment_id: int) -> ContractAttachment | None:
    """按ID查询附件"""
    result = await db.execute(
        select(ContractAttachment).where(
            ContractAttachment.id == attachment_id,
            ContractAttachment.contract_id == contract_id,
        )
    )
    return result.scalar_one_or_none()


async def add_attachment(db: AsyncSession, attachment: ContractAttachment) -> None:
    """新增附件"""
    db.add(attachment)


async def delete_attachment(db: AsyncSession, attachment: ContractAttachment) -> None:
    """删除附件"""
    await db.delete(attachment)
