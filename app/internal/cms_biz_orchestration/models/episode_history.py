"""
单集操作历史模型。

记录 EPISODE 类型内容的增删操作，用于 Episodes 弹框的 Episode History Tab。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EpisodeHistory(Base):
    """
    单集操作历史表。

    字段：
        id              主键
        parent_id       父级内容 ID（SERIES/SEASON）
        content_id      单集内容 ID
        content_name    单集名称
        content_type    内容类型（固定为 EPISODE）
        processed_by    处理人用户名（格式：username(user_id)）
        processed_type  操作类型：Add / Delete
        created_at      创建时间
        updated_at      更新时间
        is_deleted      软删除标记
        created_by      创建人ID
        updated_by      更新人ID
    """

    __tablename__ = "episode_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="父级内容 ID（SERIES/SEASON），FK → content.id",
    )
    content_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
        comment="单集内容 ID",
    )
    content_name: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="单集名称"
    )
    content_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="EPISODE", server_default="EPISODE", comment="内容类型（EPISODE/SERIES）"
    )
    series_ordinal: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Series 序号（仅 SERIES 类型使用）"
    )
    processed_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="处理人用户名（格式：username(user_id)）"
    )
    processed_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="操作类型: Add / Delete"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="软删除标记",
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="创建人ID",
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"),
        nullable=True,
        comment="更新人ID",
    )
