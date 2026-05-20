"""
单集操作历史 Pydantic 数据模型。

用于 Episodes 弹框 Episode History Tab 的数据交互。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class EpisodeHistoryItem(BaseModel):
    """单集操作历史响应项。"""

    id: int
    parent_id: int
    content_id: int
    content_name: str
    content_type: str
    series_ordinal: Optional[int] = None
    processed_by: Optional[str] = None
    processed_type: str
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class EpisodeHistoryListResponse(BaseModel):
    """单集操作历史列表响应。"""

    items: list[EpisodeHistoryItem]
    total: int


class EpisodeHistoryQueryParams(BaseModel):
    """单集操作历史查询参数。"""

    content_name: Optional[str] = None
    processed_type: Optional[str] = None
    processed_by: Optional[str] = None
