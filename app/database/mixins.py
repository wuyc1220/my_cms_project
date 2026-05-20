"""
数据库模型混入类（Mixins）。

提供统一的审计字段和通用功能，供所有业务模型继承使用。
"""
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, func
from sqlalchemy.orm import Mapped, mapped_column, declared_attr


def utcnow() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(timezone.utc)


class AuditMixin:
    """
    完整审计字段混入类。

    包含所有业务表必需的标准字段：
    - id: 主键
    - is_deleted: 软删除标记
    - created_at: 创建时间
    - updated_at: 更新时间
    - created_by: 创建人ID
    - updated_by: 更新人ID
    """

    @declared_attr.directive
    def __tablename__(cls) -> str:
        """自动生成表名（子类可覆盖）。"""
        return cls.__name__.lower()

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="主键ID"
    )

    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        comment="软删除标记: true-已删除, false-未删除",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="创建人ID，FK → cms_user.id",
    )

    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="更新人ID，FK → cms_user.id",
    )


class SoftDeleteMixin:
    """
    软删除混入类（仅包含软删除标记）。

    适用于不需要完整审计字段但需要软删除功能的表。
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        comment="软删除标记: true-已删除, false-未删除",
    )


class TimestampMixin:
    """
    时间戳混入类（仅包含创建时间和更新时间）。

    适用于需要记录时间但不需要软删除的表（如配置表、字典表）。
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )


class SimpleAuditMixin(SoftDeleteMixin, TimestampMixin):
    """
    简单审计混入类（软删除 + 时间戳，不含操作人）。

    适用于不需要记录操作人但需要软删除和时间戳的表。
    """
    pass


class AuditMixinWithoutPK:
    """
    审计字段混入类（不含主键）。

    适用于关联表（中间表），这些表通常使用复合主键。
    """

    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        comment="软删除标记: true-已删除, false-未删除",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
    )

    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="创建人ID，FK → cms_user.id",
    )

    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="更新人ID，FK → cms_user.id",
    )
