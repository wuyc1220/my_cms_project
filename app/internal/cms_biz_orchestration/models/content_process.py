"""
内容处理流程模型。

存储内容生命周期中的各个处理节点（缺失材料、补充元数据、上传海报、内容审核等）。
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ContentProcess(Base):
    """
    内容处理流程表。

    字段：
        id              主键
        content_id      内容 ID
        name            流程节点名称
        sequence        顺序号
        start_dt        开始时间
        end_dt          结束时间
        processed_before 前置处理是否完成
        status          流程状态：Pending / Passed / Finished / Failed
        assigned        分配人
        info            备注信息
        created_at      创建时间
        updated_at      更新时间
    """

    __tablename__ = "content_process"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="内容 ID，FK → content.id",
    )
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="流程节点名称"
    )
    node_code: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="节点编码，与 workflow_node_config.node_code 对应"
    )
    sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="顺序号"
    )
    start_dt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="开始时间"
    )
    end_dt: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="结束时间"
    )
    processed_before: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="前置处理是否完成"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="Pending", server_default="Pending", comment="流程状态"
    )
    assigned: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="分配人"
    )
    info: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="备注信息"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )

    # ── 审计字段 ───────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    is_discarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="废弃标识"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )
