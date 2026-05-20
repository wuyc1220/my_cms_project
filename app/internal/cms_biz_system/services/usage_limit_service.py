"""
使用限制（UsageLimit）业务逻辑层。

职责：
- 查询所有使用限制（含动态计算的当前使用量）
- 批量更新使用限制
- 获取指定类型限制值（供其他 service 调用）

业务规则：
1. content_count / storage_capacity 标记为 is_developed=False，不允许修改
2. limit_value=-1 表示不限制
3. 供应商数量限制校验：查询 provider 表中 is_deleted=false 的记录数
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException
from app.common.core.i18n import get_msg
from app.internal.cms_biz_scp.models.trade import Provider
from ..models.usage_limit import UsageLimit
from app.internal.cms_biz_system.schemas.usage_limit import (
    UsageLimitItem,
    UsageLimitsResponse,
    UsageLimitsUpdateRequest,
)

# 已开发的功能项：允许修改限制值
DEVELOPED_LIMIT_TYPES = {"supplier_count"}

async def get_usage_limits(db: AsyncSession) -> UsageLimitsResponse:
    """
    查询所有使用限制（含当前使用量）。

    输出：
        UsageLimitsResponse
    """
    limits = (
        await db.execute(select(UsageLimit).order_by(UsageLimit.id.desc()))
    ).scalars().all()

    items: list[UsageLimitItem] = []
    for limit in limits:
        current_value = await _get_current_value(db, limit.limit_type)
        items.append(
            UsageLimitItem(
                id=limit.id,
                limit_type=limit.limit_type,
                limit_value=limit.limit_value,
                current_value=current_value,
                description=limit.description,
                is_developed=limit.limit_type in DEVELOPED_LIMIT_TYPES,
                created_at=limit.created_at,
                updated_at=limit.updated_at,
            )
        )

    return UsageLimitsResponse(items=items)

async def update_usage_limits(
    db: AsyncSession, data: UsageLimitsUpdateRequest
) -> UsageLimitsResponse:
    """
    批量更新使用限制。

    输入参数：
        data    UsageLimitsUpdateRequest
    业务规则：
        - 不允许修改 is_developed=False 的功能项
    输出：
        更新后的 UsageLimitsResponse
    """
    for item in data.items:
        if item.limit_type not in DEVELOPED_LIMIT_TYPES:
            raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("USAGE_LIMIT_NOT_DEVELOPED", limit_type=item.limit_type))

        limit = (
            await db.execute(
                select(UsageLimit).where(UsageLimit.limit_type == item.limit_type)
            )
        ).scalar_one_or_none()
        if limit is None:
            raise NotFoundException(ErrorCode.NOT_FOUND, get_msg("USAGE_LIMIT_TYPE_NOT_FOUND", limit_type=item.limit_type))
        limit.limit_value = item.limit_value

    await db.commit()
    return await get_usage_limits(db)

async def get_limit_value(
    db: AsyncSession, limit_type: str, default: int = -1
) -> int:
    """
    获取指定类型的限制值（工具函数，供其他 service 调用）。

    输入参数：
        limit_type  限制类型，如 "supplier_count"
        default     找不到记录时的默认值
    输出：
        int: 限制值，-1 表示不限制
    """
    limit = (
        await db.execute(
            select(UsageLimit).where(UsageLimit.limit_type == limit_type)
        )
    ).scalar_one_or_none()
    if limit is None:
        return default
    return limit.limit_value

async def _get_current_value(db: AsyncSession, limit_type: str) -> int:
    """
    根据限制类型动态计算当前使用量。

    输入参数：
        limit_type  限制类型
    输出：
        int: 当前使用量
    """
    if limit_type == "supplier_count":
        count = (
            await db.execute(
                select(func.count()).select_from(Provider).where(Provider.is_deleted.is_(False))
            )
        ).scalar_one()
        return count
    # content_count / storage_capacity 待开发，返回占位值
    return 0
