"""
单集操作历史业务逻辑层。

职责：
- 记录单集（EPISODE）的增删操作历史
- 按 parent_id 查询单集操作历史列表
- 支持按内容名称、操作类型、处理人筛选
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.episode_history import EpisodeHistory
from app.internal.cms_biz_orchestration.schemas.episode_history import (
    EpisodeHistoryItem,
    EpisodeHistoryListResponse,
)
from app.internal.cms_biz_orchestration.repositories import episode_history_repo


async def add_episode_history(
    db: AsyncSession,
    parent_id: int,
    content_id: int,
    content_name: str,
    processed_by: str | None,
    processed_type: str,
    content_type: str = "EPISODE",
    series_ordinal: int | None = None,
    created_by: int | None = None,
) -> EpisodeHistory:
    """记录内容操作历史（支持 EPISODE 和 SERIES）。

    Args:
        db: 数据库会话
        parent_id: 父级内容 ID（SERIES/SEASON）
        content_id: 子内容 ID（EPISODE/SERIES）
        content_name: 内容名称
        processed_by: 处理人用户名（格式：username(user_id)）
        processed_type: 操作类型（Add/Delete）
        content_type: 内容类型（EPISODE/SERIES）
        series_ordinal: Series 序号（仅 SERIES 类型使用）
        created_by: 创建人ID

    Returns:
        创建的历史记录
    """
    history = EpisodeHistory(
        parent_id=parent_id,
        content_id=content_id,
        content_name=content_name,
        content_type=content_type,
        series_ordinal=series_ordinal,
        processed_by=processed_by,
        processed_type=processed_type,
        created_by=created_by,
    )
    await episode_history_repo.add_episode_history(db, history)
    return history


async def list_episode_history(
    db: AsyncSession,
    parent_id: int,
    content_name: str | None = None,
    processed_type: str | None = None,
    processed_by: str | None = None,
) -> EpisodeHistoryListResponse:
    """查询单集操作历史列表。

    Args:
        db: 数据库会话
        parent_id: 父级内容 ID
        content_name: 内容名称筛选（模糊匹配）
        processed_type: 操作类型筛选（Add/Delete）
        processed_by: 处理人筛选（模糊匹配）

    Returns:
        历史记录列表响应
    """
    histories = await episode_history_repo.list_episode_history_by_parent_id_with_filters(
        db,
        parent_id=parent_id,
        content_name=content_name,
        processed_type=processed_type,
        processed_by=processed_by,
    )
    items = [EpisodeHistoryItem.model_validate(h) for h in histories]
    return EpisodeHistoryListResponse(items=items, total=len(items))
