"""
媒资操作历史 Pydantic 数据模型。

用于 Materials 弹框 Material History Tab 的数据交互。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class MovieHistoryItem(BaseModel):
    """媒资操作历史响应项。"""

    id: int
    content_id: int
    file_name: str
    movie_type: int
    file_size: int
    processed_by: Optional[str] = None
    processed_type: str
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MovieHistoryListItem(BaseModel):
    """媒资操作历史列表响应。"""

    items: list[MovieHistoryItem]
    total: int
