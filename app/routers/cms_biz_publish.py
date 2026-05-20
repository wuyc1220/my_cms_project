"""
内容发布模块路由
包含：发布管理、注入历史
"""
from fastapi import APIRouter

from app.internal.cms_biz_publish.api.publishes import router as publishes_router

router = APIRouter(tags=["内容发布"])

router.include_router(publishes_router)
