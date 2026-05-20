"""
CP/SP管理模块路由
包含：供应商、合同、许可证
"""
from fastapi import APIRouter

from app.internal.cms_biz_scp.api.providers import router as providers_router
from app.internal.cms_biz_scp.api.contracts import router as contracts_router
from app.internal.cms_biz_scp.api.licenses import router as licenses_router

router = APIRouter(tags=["CP/SP管理"])

router.include_router(providers_router)
router.include_router(contracts_router)
router.include_router(licenses_router)
