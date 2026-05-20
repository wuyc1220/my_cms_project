"""内容数据权限模型

content_auth 表：记录内容被授权给哪些角色和用户。
每条记录表示一个 content + role/user 的授权关系。
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class ContentAuth(Base):
    """内容数据权限关联表"""
    __tablename__ = "content_auth"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True,
        comment="内容ID，FK → content.id"
    )
    role_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("role.id", ondelete="CASCADE"), nullable=True, index=True,
        comment="角色ID，FK → role.id，与 user_id 二选一"
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cms_user.id", ondelete="CASCADE"), nullable=True, index=True,
        comment="用户ID，FK → cms_user.id，与 role_id 二选一"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false",
        comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True,
        comment="操作人用户ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True,
        comment="更新人用户ID"
    )
