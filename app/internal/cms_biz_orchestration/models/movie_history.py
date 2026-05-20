"""
媒资操作历史模型。

记录 movie 表的增删操作，用于 Materials 弹框的 Material History Tab。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MovieHistory(Base):
    """
    媒资操作历史表。

    字段：
        id              主键
        content_id      所属内容 ID
        file_name       文件名
        movie_type      媒资类型：1=Movie, 2=Trailer, 3=Subtitle
        file_size       文件大小（字节）
        processed_by    处理人用户名
        processed_type  操作类型：Add / Delete
        created_at      操作时间
    """

    __tablename__ = "movie_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属内容 ID，FK → content.id",
    )
    file_name: Mapped[str] = mapped_column(String(500), nullable=False, comment="文件名")
    movie_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="媒资类型: 1=Movie, 2=Trailer, 3=Subtitle"
    )
    file_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="文件大小（字节）"
    )
    processed_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="处理人用户名"
    )
    processed_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="操作类型: Add / Delete"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="操作时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )

    # ── 审计字段 ───────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )
