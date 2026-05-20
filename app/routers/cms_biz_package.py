"""
内容打包模块路由
包含：服务包管理、任务管理
"""
from fastapi import APIRouter

from app.internal.cms_biz_package.api.packages import router as packages_router
from app.internal.cms_biz_package.api.tasks import router as tasks_router

router = APIRouter(tags=["内容打包"])

router.include_router(packages_router)
router.include_router(tasks_router)
