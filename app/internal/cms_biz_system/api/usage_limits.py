"""
使用限制（UsageLimit）API 路由层。

路由前缀：/usage-limits

接口列表：
    GET  /       获取所有使用限制（含当前使用量）
    PUT  /       批量更新使用限制
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.usage_limit import UsageLimitsResponse, UsageLimitsUpdateRequest
from app.internal.cms_biz_system.services import usage_limit_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log

router = APIRouter(prefix="/usage-limits")


@router.get("/", response_model=UsageLimitsResponse)
async def get_usage_limits(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """获取所有使用限制（含当前使用量）。"""
    return await usage_limit_service.get_usage_limits(db)


@router.put("/", response_model=UsageLimitsResponse)
async def update_usage_limits(
    body: UsageLimitsUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量更新使用限制。"""
    result = await usage_limit_service.update_usage_limits(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USAGE_LIMIT_UPDATE,
        operation_object="使用限制配置",
        operation_content=f"Updated usage limits: {[(i.limit_type, i.limit_value) for i in body.items]}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="usage_limit",
    )
    await db.commit()
    return result
