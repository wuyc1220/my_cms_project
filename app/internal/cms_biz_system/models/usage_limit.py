"""
使用限制（UsageLimit）模型。

字段：
    id              主键
    limit_type      限制类型枚举（supplier_count / content_count / storage_capacity）
    limit_value     限制值，-1 表示不限制
    description     限制的详细描述
    is_deleted      软删除标记
    created_at      创建时间
    updated_at      更新时间
    created_by      创建人ID
    updated_by      更新人ID
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UsageLimit(Base):
    __tablename__ = "usage_limit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    limit_type: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    limit_value: Mapped[int] = mapped_column(Integer, nullable=False, default=-1)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
