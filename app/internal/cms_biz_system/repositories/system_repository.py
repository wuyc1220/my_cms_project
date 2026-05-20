"""
系统管理模块 - 数据访问层（补充）
Config / Dict / OperationLog / SensitiveWord / UsageLimit 的 SQLAlchemy 操作
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_system.models.config import Config
from app.internal.cms_biz_system.models.dict import DictNode
from app.internal.cms_biz_system.models.operation_log import OperationLog
from app.internal.cms_biz_system.models.sensitive_word import SensitiveWord
from app.internal.cms_biz_system.models.usage_limit import UsageLimit


# ═══════════════════════════════════════════════════════════
# Config
# ═══════════════════════════════════════════════════════════

async def get_config_by_id(db: AsyncSession, config_id: int) -> Config | None:
    return (await db.execute(select(Config).where(Config.id == config_id))).scalar_one_or_none()


async def get_config_by_key(db: AsyncSession, config_key: str) -> Config | None:
    return (await db.execute(select(Config).where(Config.config_key == config_key))).scalar_one_or_none()


async def list_configs_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    config_key: str | None = None, config_name: str | None = None,
    description: str | None = None,
    sort_by: str | None = None, sort_order: str | None = None,
) -> tuple[list[Config], int]:
    query = select(Config)
    if config_key:
        query = query.where(Config.config_key.ilike(f"%{config_key}%"))
    if config_name:
        query = query.where(Config.config_name.ilike(f"%{config_name}%"))
    if description:
        query = query.where(Config.description.ilike(f"%{description}%"))

    if sort_by and sort_order:
        sort_column = getattr(Config, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(Config.id.desc())
    else:
        query = query.order_by(Config.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def add_config(db: AsyncSession, config: Config) -> None:
    db.add(config)


async def delete_config(db: AsyncSession, config: Config) -> None:
    await db.delete(config)


# ═══════════════════════════════════════════════════════════
# Dict
# ═══════════════════════════════════════════════════════════

async def get_dict_by_id(db: AsyncSession, dict_id: int) -> DictNode | None:
    return (await db.execute(select(DictNode).where(DictNode.id == dict_id, DictNode.is_deleted == False))).scalar_one_or_none()


async def get_dict_by_code(db: AsyncSession, code: str) -> DictNode | None:
    return (await db.execute(select(DictNode).where(DictNode.code == code, DictNode.is_deleted == False))).scalar_one_or_none()


async def get_root_dicts(db: AsyncSession) -> list[DictNode]:
    return (await db.execute(
        select(DictNode).where(DictNode.parent_id.is_(None), DictNode.is_deleted == False).order_by(DictNode.sort_order, DictNode.id)
    )).scalars().all()


async def get_dict_children(db: AsyncSession, parent_id: int) -> list[DictNode]:
    return (await db.execute(
        select(DictNode).where(DictNode.parent_id == parent_id, DictNode.is_deleted == False).order_by(DictNode.sort_order, DictNode.id)
    )).scalars().all()


async def add_dict(db: AsyncSession, node: DictNode) -> None:
    db.add(node)


async def delete_dict(db: AsyncSession, node: DictNode) -> None:
    await db.delete(node)


# ═══════════════════════════════════════════════════════════
# OperationLog
# ═══════════════════════════════════════════════════════════

async def list_operation_logs_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    user_name: str | None = None, operation_type: str | None = None,
    start_date: str | None = None, end_date: str | None = None,
) -> tuple[list[OperationLog], int]:
    query = select(OperationLog)
    if user_name:
        query = query.where(OperationLog.user_name.ilike(f"%{user_name}%"))
    if operation_type:
        query = query.where(OperationLog.operation_type == operation_type)
    if start_date:
        query = query.where(OperationLog.created_at >= start_date)
    if end_date:
        query = query.where(OperationLog.created_at <= end_date)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(
        query.order_by(OperationLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return result.scalars().all(), total


async def add_operation_log(db: AsyncSession, log: OperationLog) -> None:
    db.add(log)


# ═══════════════════════════════════════════════════════════
# SensitiveWord
# ═══════════════════════════════════════════════════════════

async def get_sensitive_word_by_id(db: AsyncSession, word_id: int) -> SensitiveWord | None:
    return (await db.execute(select(SensitiveWord).where(SensitiveWord.id == word_id))).scalar_one_or_none()


async def list_sensitive_words_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    word: str | None = None, category: str | None = None,
    level: str | None = None,
) -> tuple[list[SensitiveWord], int]:
    query = select(SensitiveWord)
    if word:
        query = query.where(SensitiveWord.word.ilike(f"%{word}%"))
    if category:
        query = query.where(SensitiveWord.category == category)
    if level:
        query = query.where(SensitiveWord.level == level)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.order_by(SensitiveWord.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_all_sensitive_words(db: AsyncSession) -> list[SensitiveWord]:
    return (await db.execute(select(SensitiveWord).order_by(SensitiveWord.id))).scalars().all()


async def get_sensitive_words_by_ids(db: AsyncSession, ids: list[int]) -> list[SensitiveWord]:
    return (await db.execute(select(SensitiveWord).where(SensitiveWord.id.in_(ids)))).scalars().all()


async def add_sensitive_word(db: AsyncSession, sw: SensitiveWord) -> None:
    db.add(sw)


async def delete_sensitive_word(db: AsyncSession, sw: SensitiveWord) -> None:
    await db.delete(sw)


# ═══════════════════════════════════════════════════════════
# UsageLimit
# ═══════════════════════════════════════════════════════════

async def get_usage_limit_by_id(db: AsyncSession, limit_id: int) -> UsageLimit | None:
    return (await db.execute(select(UsageLimit).where(UsageLimit.id == limit_id))).scalar_one_or_none()


async def get_usage_limit_by_key(db: AsyncSession, limit_key: str) -> UsageLimit | None:
    return (await db.execute(select(UsageLimit).where(UsageLimit.limit_key == limit_key))).scalar_one_or_none()


async def list_usage_limits_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    limit_key: str | None = None, limit_name: str | None = None,
) -> tuple[list[UsageLimit], int]:
    query = select(UsageLimit)
    if limit_key:
        query = query.where(UsageLimit.limit_key.ilike(f"%{limit_key}%"))
    if limit_name:
        query = query.where(UsageLimit.limit_name.ilike(f"%{limit_name}%"))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.order_by(UsageLimit.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_all_usage_limits(db: AsyncSession) -> list[UsageLimit]:
    return (await db.execute(select(UsageLimit).order_by(UsageLimit.id))).scalars().all()


async def add_usage_limit(db: AsyncSession, ul: UsageLimit) -> None:
    db.add(ul)


async def delete_usage_limit(db: AsyncSession, ul: UsageLimit) -> None:
    await db.delete(ul)
