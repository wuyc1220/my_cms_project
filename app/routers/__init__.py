"""
路由层 - 按业务模块组织

模块路由将旧api路由按业务域重新组织，增加模块前缀：
  /api/v1/system/...      ← cms_biz_system
  /api/v1/scp/...         ← cms_biz_scp
  /api/v1/metada/...      ← cms_biz_metada
  /api/v1/orchestration/.. ← cms_biz_orchestration
  /api/v1/package/...     ← cms_biz_package
  /api/v1/flow/...        ← cms_biz_flow
  /api/v1/dashboard/...   ← cms_biz_stat

旧路由 /api/v1/users 等通过 legacy_api_router 保持兼容。
"""
from fastapi import APIRouter

from app.routers.cms_biz_system import router as system_router
from app.routers.cms_biz_scp import router as scp_router
from app.routers.cms_biz_metada import router as metada_router
from app.routers.cms_biz_orchestration import router as orchestration_router
from app.routers.cms_biz_package import router as package_router
from app.routers.cms_biz_publish import router as publish_router
from app.routers.cms_biz_flow import router as flow_router
from app.routers.cms_biz_stat import router as stat_router

# 创建总路由
api_router = APIRouter()

# 注册所有业务模块路由（不带前缀，保持原有路径）
api_router.include_router(system_router)
api_router.include_router(scp_router)
api_router.include_router(metada_router)
api_router.include_router(orchestration_router)
api_router.include_router(package_router)
api_router.include_router(publish_router)
api_router.include_router(flow_router)
api_router.include_router(stat_router)