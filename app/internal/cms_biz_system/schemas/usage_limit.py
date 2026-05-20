"""
使用限制（UsageLimit）Pydantic 数据结构。

包含：
- UsageLimitItem: 单条使用限制（含动态计算的当前使用量）
- UsageLimitsResponse: 使用限制列表响应
- UsageLimitUpdateItem: 更新请求中的单条
- UsageLimitsUpdateRequest: 批量更新请求
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_core import PydanticCustomError

from app.common.core.i18n import get_msg


class UsageLimitItem(BaseModel):
    """单条使用限制项，含动态计算的当前使用量和开发状态。"""

    id: int
    limit_type: str
    limit_value: int
    current_value: int = 0
    description: str | None = None
    is_developed: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UsageLimitsResponse(BaseModel):
    """使用限制列表响应。"""

    items: list[UsageLimitItem]


class UsageLimitUpdateItem(BaseModel):
    """更新请求中的单条：指定限制类型和新限制值。"""

    limit_type: str
    limit_value: int

    @field_validator('limit_value', mode='before')
    @classmethod
    def validate_integer(cls, v):
        if isinstance(v, float) and v % 1 != 0:
            raise PydanticCustomError('integer_required', get_msg('VALIDATION_INTEGER_REQUIRED'))
        return v


class UsageLimitsUpdateRequest(BaseModel):
    """批量更新使用限制请求。"""

    items: list[UsageLimitUpdateItem]
