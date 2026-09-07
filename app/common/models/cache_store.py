"""
cache_store 表 ORM 模型

UNLOGGED 表，用于存储短期缓存数据（验证码、临时令牌等）。
与 Redis 类似，支持 key-value + TTL 过期机制，但基于 PostgreSQL 实现，
天然支持多实例共享和服务重启不丢失。

注意：created_by / updated_by 保留字段但不声明 ORM 外键，
FK 约束由数据库 DDL 层面维护，ORM 层不重复声明以避免模型加载顺序依赖。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CacheStore(Base):
    __tablename__ = "cache_store"

    key: Mapped[str] = mapped_column(String(255), primary_key=True, nullable=False)
    value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=True
    )
    is_deleted: Mapped[bool | None] = mapped_column(Boolean, default=False, server_default="false", nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True
    )
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
