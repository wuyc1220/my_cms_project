"""
任务管理模型。

包含：
- Task：任务主表（内容编排/审核任务）
- TaskHistory：任务操作历史记录表
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Task(Base):
    """
    任务主表。

    字段：
        id              主键
        content_id      内容 ID
        task_type       任务类型：arrangement / review L1 / review L2 / review L3
        assignee_id     分配人用户ID
        task_status     任务状态：Not Assigned / Pending / Completed
        start_time      开始时间
        end_time        结束时间
        created_at      创建时间
        updated_at      更新时间
    """

    __tablename__ = "task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="内容 ID，FK → content.id",
    )
    task_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="任务类型：arrangement / review L1 / review L2 / review L3"
    )
    assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="分配人用户ID，FK → cms_user.id",
    )
    task_status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="Not Assigned",
        server_default="Not Assigned",
        comment="任务状态：Not Assigned / Pending / Completed",
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, comment="开始时间"
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false"), nullable=False, comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class TaskHistory(Base):
    """
    任务操作历史记录表。

    字段：
        id              主键
        task_id         任务 ID
        processed_type  操作类型：Add / Update / Delete / Assign
        processed_by    操作人用户名
        processed_at    操作时间
        previous_value  修改前值
        updated_value   修改后值
    """

    __tablename__ = "task_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("task.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="任务 ID，FK → task.id",
    )
    processed_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="操作类型：Add / Update / Delete / Assign"
    )
    processed_by: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="操作人用户名"
    )
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="操作时间"
    )
    previous_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="修改前值"
    )
    updated_value: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="修改后值"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false"), nullable=False, comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
