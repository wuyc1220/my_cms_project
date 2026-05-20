"""
人物角色关联 Pydantic 数据模型。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class CastRoleMapBase(BaseModel):
    """人物角色关联基础字段。"""
    cast_id: int
    role_name: Optional[str] = Field(None, max_length=100)
    role_code: Optional[str] = Field(None, max_length=100)


class CastRoleMapCreate(CastRoleMapBase):
    """创建人物角色关联请求体。"""
    content_id: Optional[int] = None
    program_id: Optional[int] = None
    movie_id: Optional[int] = None


class CastRoleMapUpdate(BaseModel):
    """更新人物角色关联请求体。"""
    role_name: Optional[str] = Field(None, max_length=100)
    role_code: Optional[str] = Field(None, max_length=100)


class CastRoleMapItem(CastRoleMapBase):
    """人物角色关联响应项。"""
    map_id: int
    program_id: Optional[int] = None
    movie_id: Optional[int] = None
    content_id: Optional[int] = None
    cast_name: Optional[str] = None
    cast_poster_url: Optional[str] = None
    is_deleted: Optional[bool] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class CastRoleMapListItem(BaseModel):
    """人物角色关联列表响应。"""
    items: list[CastRoleMapItem]
    total: int
