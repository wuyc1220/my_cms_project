"""
流程编排模块路由
包含：工作流配置、流程编排、流程执行
"""
from fastapi import APIRouter

from app.internal.cms_biz_flow.api.workflow_config import router as workflow_config_router

router = APIRouter(tags=["流程编排"])

router.include_router(workflow_config_router)
