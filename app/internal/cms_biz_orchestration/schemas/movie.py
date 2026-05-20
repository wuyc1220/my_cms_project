"""
媒资实体 Pydantic 数据模型。

覆盖 Movie / Trailer / Subtitle 三种媒资类型的 CRUD。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


# ═══════════════════════════════════════════════════════════
# Movie — 媒资实体
# ═══════════════════════════════════════════════════════════

class MovieBase(BaseModel):
    """媒资实体基础字段。"""
    file_name: str
    file_path: str
    file_size: int = 0
    movie_type: int
    sequence: Optional[int] = None
    audio_type: Optional[str] = None
    screen_format: Optional[str] = None
    closed_captioning: bool = True
    duration: int
    definition: str
    mediaservice: Optional[str] = None
    encryption: bool = True
    publish_flag: bool = True
    deeplink: Optional[str] = None


class MovieCreate(MovieBase):
    """创建媒资实体请求体。"""
    content_id: int


class MovieUpdate(BaseModel):
    """更新媒资实体请求体（所有字段可选）。"""
    file_name: Optional[str] = None
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    movie_type: Optional[int] = None
    sequence: Optional[int] = None
    audio_type: Optional[str] = None
    screen_format: Optional[str] = None
    closed_captioning: Optional[bool] = None
    duration: Optional[int] = None
    definition: Optional[str] = None
    mediaservice: Optional[str] = None
    encryption: Optional[bool] = None
    publish_flag: Optional[bool] = None
    deeplink: Optional[str] = None


class MovieItem(MovieBase):
    """媒资实体响应项。"""
    id: int
    content_id: int
    created_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class MovieListItem(BaseModel):
    """媒资实体列表响应（用于内容详情页 Media File Tab）。"""
    items: list[MovieItem]
    total: int
