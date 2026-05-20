"""
内容审批记录模型。

存储内容的多级审批记录，支持 L1/L2/L3 多级审批流程。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContentReview(Base):
    """
    内容审批记录表。

    字段：
        id                  主键
        content_id          内容 ID
        review_level        审批层级：L1/L2/L3/None(免审批)
        level_required      需要的审批层级数（1/2/3/0）
        level_1_status      L1审批状态：Pending/Passed/Rejected
        level_1_by          L1审批人
        level_1_at          L1审批时间
        level_1_comment     L1审批意见
        level_2_status      L2审批状态
        level_2_by          L2审批人
        level_2_at          L2审批时间
        level_2_comment     L2审批意见
        level_3_status      L3审批状态
        level_3_by          L3审批人
        level_3_at          L3审批时间
        level_3_comment     L3审批意见
        final_status        最终审批状态：Pending/Passed/Rejected
        initiated_by        发起人
        initiated_at        发起时间
        completed_at        完成时间
        created_at          创建时间
        updated_at          更新时间
    """

    __tablename__ = "content_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="内容 ID，FK → content.id",
    )
    review_level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="None", server_default="None",
        comment="审批层级：None(免审批)/L1/L2/L3"
    )
    level_required: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="需要的审批层级数：0(免审批)/1/2/3"
    )
    
    # L1 审批
    level_1_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Pending", server_default="Pending",
        comment="L1审批状态：Pending/Passed/Rejected"
    )
    level_1_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="L1审批人用户名"
    )
    level_1_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="L1审批时间"
    )
    level_1_comment: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="L1审批意见"
    )
    
    # L2 审批
    level_2_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Pending", server_default="Pending",
        comment="L2审批状态：Pending/Passed/Rejected"
    )
    level_2_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="L2审批人用户名"
    )
    level_2_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="L2审批时间"
    )
    level_2_comment: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="L2审批意见"
    )
    
    # L3 审批
    level_3_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Pending", server_default="Pending",
        comment="L3审批状态：Pending/Passed/Rejected"
    )
    level_3_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="L3审批人用户名"
    )
    level_3_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="L3审批时间"
    )
    level_3_comment: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="L3审批意见"
    )
    
    # 最终状态
    final_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Pending", server_default="Pending",
        comment="最终审批状态：Pending/Passed/Rejected"
    )
    initiated_by: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="发起人用户名"
    )
    initiated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="发起时间"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )
