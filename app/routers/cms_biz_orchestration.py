"""
内容采编模块路由
包含：内容管理、直播管理、点播管理、海报图片、元数据管理、媒资管理
"""
from fastapi import APIRouter

from app.internal.cms_biz_orchestration.api.contents import router as contents_router
from app.internal.cms_biz_orchestration.api.live import router as live_router
from app.internal.cms_biz_orchestration.api.vod import router as vod_router
from app.internal.cms_biz_orchestration.api.pictures import router as pictures_router
from app.internal.cms_biz_orchestration.api.picture_publish import router as picture_publish_router
from app.internal.cms_biz_orchestration.api.metadata import router as metadata_router
from app.internal.cms_biz_orchestration.api.movie import router as movie_router
from app.internal.cms_biz_orchestration.api.cast_role_map import router as cast_role_map_router
from app.internal.cms_biz_orchestration.api.ingest_history import router as ingest_history_router
from app.internal.cms_biz_orchestration.api.episode_history import router as episode_history_router

router = APIRouter(tags=["内容采编"])

router.include_router(contents_router)
router.include_router(live_router)
router.include_router(vod_router)
router.include_router(pictures_router)
router.include_router(picture_publish_router)
router.include_router(metadata_router)
router.include_router(movie_router)
router.include_router(cast_role_map_router)
router.include_router(ingest_history_router)
router.include_router(episode_history_router)
