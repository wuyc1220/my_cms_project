"""
内容状态变更日志模型。

记录内容 Ingest 状态的每次变更历史（Before → After）。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContentStatusLog(Base):
    """
    内容状态变更日志表。

    字段：
        id              主键
        content_id      内容 ID
        before_status   变更前状态
        after_status    变更后状态
        processed_by    处理人（用户名）
        processed_at    处理时间
        created_at      记录创建时间
    """

    __tablename__ = "content_status_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="内容 ID，FK → content.id",
    )
    before_status: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="变更前 Ingest 状态"
    )
    after_status: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="变更后 Ingest 状态"
    )
    processed_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="处理人用户名"
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="处理时间"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="记录创建时间"
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
