"""
发布任务（Publish Task）领域模型。

记录内容/频道/节目单的发布/下架任务，支持立即执行和计划执行。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class PublishTask(Base):
    """
    发布任务表。

    字段：
        id              主键
        entity_type     实体类型: Content / Channel / Schedule
        entity_id       实体ID
        entity_name     实体名称
        task_type       任务类型: publish / unpublish
        execution_mode  执行方式: now / plan
        scheduled_time  计划执行时间（execution_mode=plan时有效）
        status          任务状态: pending / processing / success / failure / cancelled
        publish_status  发布状态: none / plan / publishing / success / failure / closed
        publish_time    实际发布时间
        unpublish_time  实际下架时间
        error_message   错误信息（失败时记录）
        correlate_id    SOAP 关联 ID，用于匹配 LSP 结果通知
        ingest_xml_path Ingest XML 文件路径
        result_xml_path Result XML 文件路径
        created_by      创建人ID
        created_at      创建时间
        updated_at      更新时间
    """

    __tablename__ = "publish_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="实体类型: Content/Channel/Schedule"
    )
    entity_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="实体ID"
    )
    entity_name: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="实体名称"
    )
    content_type: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="内容类型: MOVIE/EPISODE/SERIES/SEASON/CHANNEL/SCHEDULE"
    )
    task_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="任务类型: publish/unpublish"
    )
    execution_mode: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="执行方式: now/plan"
    )
    scheduled_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="计划执行时间"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="pending", server_default="pending",
        comment="任务状态: pending/processing/success/failure/cancelled"
    )
    publish_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="none", server_default="none",
        comment="发布状态: none/plan/publishing/success/failure/closed"
    )
    publish_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际发布时间"
    )
    unpublish_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="实际下架时间"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="错误信息"
    )
    retry_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="已重试次数（与 scheduled_task.retry_count 比对，控制 ContentPublish 定时任务跨轮重试）"
    )
    correlate_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="SOAP 关联 ID，用于匹配 LSP 结果通知"
    )
    ingest_xml_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Ingest XML 文件路径"
    )
    result_xml_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Result XML 文件路径"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
