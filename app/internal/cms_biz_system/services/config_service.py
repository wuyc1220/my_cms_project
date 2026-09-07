from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.config import Config
from app.internal.cms_biz_system.schemas.user_crud import (
    ConfigCreate,
    ConfigListItem,
    ConfigUpdate,
    LanguageConfigResponse,
)
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


async def _get_config_or_404(db: AsyncSession, config_id: int) -> Config:
    """获取配置，不存在则404"""
    config = (await db.execute(
        select(Config).where(Config.id == config_id, Config.is_deleted.is_(False))
    )).scalar_one_or_none()
    if not config:
        raise NotFoundException(ErrorCode.CONFIG_NOT_FOUND, get_msg("CONFIG_NOT_FOUND"))
    return config


async def get_config_value(db: AsyncSession, config_key: str, default_value: str = "") -> str:
    """
    获取配置值

    Args:
        db: 数据库会话
        config_key: 配置键
        default_value: 默认值

    Returns:
        str: 配置值，如果不存在则返回默认值
    """
    config = (
        await db.execute(
            select(Config).where(Config.config_key == config_key, Config.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if config and config.config_value:
        return config.config_value.strip()
    return default_value


async def get_config_int(db: AsyncSession, config_key: str, default_value: int = 0) -> int:
    """
    获取整数类型配置值

    Args:
        db: 数据库会话
        config_key: 配置键
        default_value: 默认值

    Returns:
        int: 配置值
    """
    value = await get_config_value(db, config_key, str(default_value))
    try:
        return int(value)
    except (ValueError, TypeError):
        return default_value


async def get_config_bool(db: AsyncSession, config_key: str, default_value: bool = False) -> bool:
    """
    获取布尔类型配置值

    Args:
        db: 数据库会话
        config_key: 配置键
        default_value: 默认值

    Returns:
        bool: 配置值
    """
    value = await get_config_value(db, config_key, str(default_value).lower())
    return value.lower() in ("true", "1", "yes")


async def list_configs(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    config_key: str | None = None,
    config_name: str | None = None,
    description: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[ConfigListItem]:
    query = select(Config).where(Config.is_deleted.is_(False))
    if config_key:
        query = query.where(Config.config_key.ilike(f"%{config_key}%"))
    if config_name:
        query = query.where(Config.config_name.ilike(f"%{config_name}%"))
    if description:
        query = query.where(Config.description.ilike(f"%{description}%"))

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Config, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func)
        else:
            query = query.order_by(Config.id.desc())
    else:
        query = query.order_by(Config.id.desc())

    configs = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[ConfigListItem.model_validate(c) for c in configs],
    )


async def get_config(db: AsyncSession, config_id: int) -> Config:
    return await _get_config_or_404(db, config_id)


async def get_ui_language(db: AsyncSession) -> LanguageConfigResponse:
    config = (
        await db.execute(
            select(Config).where(Config.config_key == "SYSTEM_UI_LANGUAGE", Config.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    language = (config.config_value or "").strip().lower() if config else ""
    if language not in {"cn", "en"}:
        language = "cn"
    return LanguageConfigResponse(language=language)


async def create_config(db: AsyncSession, data: ConfigCreate) -> Config:
    existing = (await db.execute(
        select(Config.id).where(Config.config_key == data.config_key, Config.is_deleted.is_(False)).limit(1)
    )).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.CONFIG_CODE_EXISTS, get_msg("CONFIG_CODE_EXISTS", code=data.config_key))
    config = Config(
        config_key=data.config_key,
        config_name=data.config_name,
        config_value=data.config_value,
        description=data.description,
        is_system=False,
    )
    db.add(config)
    await db.commit()
    await db.refresh(config)
    return config


async def update_config(db: AsyncSession, config_id: int, data: ConfigUpdate) -> Config:
    config = await _get_config_or_404(db, config_id)
    if data.config_name is not None:
        config.config_name = data.config_name
    if data.config_value is not None:
        config.config_value = data.config_value
    if data.description is not None:
        config.description = data.description
    await db.commit()
    await db.refresh(config)

    if config.config_key in ("MASTER_PLATFORM_ENABLED", "MASTER_PLATFORM_URLS"):
        from app.common.services.master_platform_service import MasterPlatformService
        from loguru import logger
        service = MasterPlatformService.get_instance()
        await service.load_config(db)
        logger.info(
            "主中心配置热更新完成 | enabled={} is_master={}",
            service.enabled,
            service.is_master,
        )

    return config


async def delete_config(db: AsyncSession, config_id: int) -> None:
    config = await _get_config_or_404(db, config_id)
    if config.is_system:
        raise BusinessException(ErrorCode.SYSTEM_CONFIG_CANNOT_DELETE, get_msg("SYSTEM_CONFIG_CANNOT_DELETE"))
    config.is_deleted = True
    await db.commit()
