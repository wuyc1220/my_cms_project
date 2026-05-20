"""公共 Pydantic 模型 — 跨模块共享。"""

from typing import Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class PaginatedResponse(BaseModel, Generic[T]):
    total: int
    page: int
    page_size: int
    items: list[T]


class BatchDeleteRequest(BaseModel):
    ids: list[int]
