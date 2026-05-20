"""
系统管理模块路由
包含：认证、用户、角色、字典、参数、日志、使用限制、敏感词、健康检查
URL路径保持 /api/v1/users, /api/v1/roles 等不变
"""
from fastapi import APIRouter

from app.internal.cms_biz_system.api.auth import router as auth_router
from app.internal.cms_biz_system.api.users import router as users_router
from app.internal.cms_biz_system.api.roles import router as roles_router
from app.internal.cms_biz_system.api.configs import router as configs_router
from app.internal.cms_biz_system.api.dicts import router as dicts_router
from app.internal.cms_biz_system.api.operation_logs import router as operation_logs_router
from app.internal.cms_biz_system.api.usage_limits import router as usage_limits_router
from app.internal.cms_biz_system.api.sensitive_words import router as sensitive_words_router
from app.internal.cms_biz_system.api.menus import router as menus_router
from app.internal.cms_biz_system.api.content_auth import router as content_auth_router
from app.internal.cms_biz_system.api.health import router as health_router
from app.internal.cms_biz_system.api.attachments import router as attachments_router
from app.internal.cms_biz_system.api.scheduled_tasks import router as scheduled_tasks_router
from app.internal.cms_biz_system.api.metadata_quality import router as metadata_quality_router
from app.internal.cms_biz_system.api.metadata_validation_rules import router as metadata_validation_rules_router

router = APIRouter(tags=["系统管理"])

# 路径保持原样：/api/v1/users, /api/v1/roles 等
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(roles_router)
router.include_router(configs_router)
router.include_router(dicts_router)
router.include_router(operation_logs_router)
router.include_router(usage_limits_router)
router.include_router(sensitive_words_router)
router.include_router(menus_router)
router.include_router(content_auth_router)
router.include_router(health_router)
router.include_router(attachments_router)
router.include_router(scheduled_tasks_router)
router.include_router(metadata_quality_router)
router.include_router(metadata_validation_rules_router)
