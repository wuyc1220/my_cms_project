"""
看板统计模块路由

包含：看板数据查询、用户配置管理
URL路径：/api/v1/dashboard/...
"""
from fastapi import APIRouter

from app.internal.cms_biz_stat.api import router as dashboard_router

router = APIRouter(tags=["看板统计"])

router.include_router(dashboard_router)
