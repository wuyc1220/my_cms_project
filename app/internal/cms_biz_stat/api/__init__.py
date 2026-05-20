"""
看板统计模块 API 路由层。

路由前缀：/dashboard

接口列表：
    GET    /                      获取看板综合数据
    GET    /config                获取用户看板配置
    PUT    /config                更新用户看板配置
    POST   /config/reset          重置用户看板配置为默认
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, get_db
from app.internal.cms_biz_stat.schemas import (
    DashboardDataResponse,
    UserDashboardConfigResponse,
    UserDashboardConfigUpdate,
)
from app.internal.cms_biz_stat.services import DashboardConfigService, DashboardStatService
from app.internal.cms_biz_system.models.user import User

router = APIRouter(prefix="/dashboard")


@router.get("/", response_model=DashboardDataResponse)
async def get_dashboard_data(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取看板综合数据"""
    stat_service = DashboardStatService(db)
    return await stat_service.get_dashboard_data(current_user.id, current_user)


@router.get("/config", response_model=UserDashboardConfigResponse)
async def get_dashboard_config(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """获取用户看板配置"""
    config_service = DashboardConfigService(db)
    result = await config_service.get_config_response(current_user.id, current_user)
    await db.commit()
    return result


@router.put("/config", response_model=UserDashboardConfigResponse)
async def update_dashboard_config(
    body: UserDashboardConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新用户看板配置"""
    config_service = DashboardConfigService(db)
    user_name = current_user.display_name or current_user.username
    result = await config_service.update_config(current_user.id, body, user_name)
    await db.commit()
    return result


@router.post("/config/reset", response_model=UserDashboardConfigResponse)
async def reset_dashboard_config(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """重置用户看板配置为默认"""
    config_service = DashboardConfigService(db)
    user_name = current_user.display_name or current_user.username
    result = await config_service.reset_config(current_user.id, user_name)
    await db.commit()
    return result
