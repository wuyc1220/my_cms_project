"""
单集操作历史 API 路由层。

路由前缀：/contents/{parent_id}/episodes/history

接口列表：
    GET    /contents/{parent_id}/episodes/history      查询单集操作历史列表
"""

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_orchestration.schemas.episode_history import (
    EpisodeHistoryListResponse,
)
from app.internal.cms_biz_orchestration.services import episode_history_service

router = APIRouter(tags=["单集操作历史"])


@router.get("/contents/{parent_id}/episodes/history", response_model=EpisodeHistoryListResponse)
async def list_episode_history(
    parent_id: int,
    content_name: str | None = Query(None, description="内容名称筛选（模糊匹配）"),
    processed_type: str | None = Query(None, description="操作类型筛选（Add/Delete）"),
    processed_by: str | None = Query(None, description="处理人筛选（模糊匹配）"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询单集操作历史列表。

    Args:
        parent_id: 父级内容 ID（SERIES/SEASON）
        content_name: 内容名称筛选（模糊匹配）
        processed_type: 操作类型筛选（Add/Delete）
        processed_by: 处理人筛选（模糊匹配）

    Returns:
        单集操作历史列表
    """
    return await episode_history_service.list_episode_history(
        db,
        parent_id=parent_id,
        content_name=content_name,
        processed_type=processed_type,
        processed_by=processed_by,
    )
