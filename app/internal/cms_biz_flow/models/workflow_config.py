"""
工作流配置领域模型。

包含：
- WorkflowConfig: 流程配置主表
- WorkflowNodeConfig: 流程节点配置表
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class WorkflowConfig(Base):
    """
    流程配置表。

    字段：
        id                主键
        process_code      流程编码
        process_name      流程名称
        belonging         所属模块: PROGRAM/SEASON/SERIES/CHANNEL/SCHEDULE
        status            状态: draft/published
        version           版本号
        published_version 已发布版本号
        config_json       流程编排JSON配置
        created_at        创建时间
        updated_at        更新时间
    """

    __tablename__ = "workflow_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    process_code: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True, comment="流程编码"
    )
    process_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="流程名称"
    )
    belonging: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True, comment="所属模块: PROGRAM/SEASON/SERIES/CHANNEL/SCHEDULE"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft", server_default="draft",
        index=True, comment="状态: draft/published/unpublished"
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1", comment="版本号"
    )
    published_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="已发布版本号"
    )
    config_json: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="流程编排JSON配置"
    )

    # 关系
    nodes: Mapped[list["WorkflowNodeConfig"]] = relationship(
        "WorkflowNodeConfig",
        back_populates="workflow_config",
        cascade="all, delete-orphan",
        order_by="WorkflowNodeConfig.sequence",
    )

    # 审计字段
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


class WorkflowNodeConfig(Base):
    """
    流程节点配置表。

    字段：
        id                主键
        workflow_config_id FK → workflow_config.id
        node_code         节点编码
        node_name         节点显示名称
        node_type         节点类型: process/parallel_box
        mandatory         是否必须环节
        parallel_rule     并行聚合规则: any_required/all_required
        bind_status_before 绑定前置状态
        bind_status_after  绑定后置状态
        position_x        画布X坐标
        position_y        画布Y坐标
        sequence          顺序号
        parent_node_id    父节点ID(并行框内的子节点)
        created_at        创建时间
        updated_at        更新时间
    """

    __tablename__ = "workflow_node_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    workflow_config_id: Mapped[int] = mapped_column(
        ForeignKey("workflow_config.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="FK → workflow_config.id",
    )
    node_code: Mapped[str] = mapped_column(
        String(100), nullable=False, index=True,
        comment="节点编码: Materials/Metadata/Posters/CastRoleMap/Category/Package/ApplicationReview/ContentReview/PublishPlan/InjectSubContent/Trailer/MusicEffects/Encoding"
    )
    node_name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="节点显示名称"
    )
    node_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="节点类型: process/parallel_box"
    )
    mandatory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
        comment="是否必须环节(不可跳过)"
    )
    parallel_rule: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="并行聚合规则: any_required/all_required"
    )
    bind_status_before: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="绑定业务状态(前)"
    )
    bind_status_after: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="绑定业务状态(后)"
    )
    position_x: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="画布X坐标"
    )
    position_y: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="画布Y坐标"
    )
    width: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="节点宽度"
    )
    height: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="节点高度"
    )
    sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="顺序号"
    )
    parent_node_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="父节点ID(并行框内的子节点)"
    )

    # 关系
    workflow_config: Mapped["WorkflowConfig"] = relationship(
        "WorkflowConfig", back_populates="nodes"
    )

    # 审计字段
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
