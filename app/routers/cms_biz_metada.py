"""
内容元数据模块路由
包含：人物、栏目、内容类型、题材、标签、自定义标签、自定义字段、海报规格、数据源管理、爬取任务管理
"""
from fastapi import APIRouter

from app.internal.cms_biz_metada.api.casts import router as casts_router
from app.internal.cms_biz_metada.api.categories import router as categories_router
from app.internal.cms_biz_metada.api.content_types import router as content_types_router
from app.internal.cms_biz_metada.api.genres import router as genres_router
from app.internal.cms_biz_metada.api.tags import router as tags_router
from app.internal.cms_biz_metada.api.custom_tags import router as custom_tags_router
from app.internal.cms_biz_metada.api.custom_fields import router as custom_fields_router
from app.internal.cms_biz_metada.api.poster_sizes import router as poster_sizes_router
from app.internal.cms_biz_metada.api.metadata_sources import router as metadata_sources_router
from app.internal.cms_biz_metada.api.crawl_tasks import router as crawl_tasks_router

router = APIRouter(tags=["内容元数据"])

router.include_router(casts_router)
router.include_router(categories_router)
router.include_router(content_types_router)
router.include_router(genres_router)
router.include_router(tags_router)
router.include_router(custom_tags_router)
router.include_router(custom_fields_router)
router.include_router(poster_sizes_router)
router.include_router(metadata_sources_router)
router.include_router(crawl_tasks_router)
