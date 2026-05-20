"""
元数据增强模块 - 数据模型

包含：
- MetadataSource：数据源配置表
- MetadataCrawlTask：爬取任务表
- MetadataCrawlDetail：爬取详情/候选值表
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MetadataSource(Base):
    """数据源配置表"""

    __tablename__ = "metadata_source"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="数据源名称，如IMDb、OMDb等")
    content_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="内容类型：Movie/Series/Cast"
    )
    collect_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default="API", server_default="API", comment="采集方式：API/网页爬取"
    )
    url: Mapped[str] = mapped_column(String(500), nullable=False, comment="数据源基础URL")
    api_endpoint: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="API接口地址"
    )
    auth_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="认证方式：API_Key/OAuth2.0/None"
    )
    api_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="API密钥，加密存储"
    )
    rate_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1000, server_default="1000", comment="请求频率限制（次/小时）"
    )
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="YES", server_default="YES", comment="状态：YES/NO"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, comment="软删除标记"
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
    page_url_template: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="爬取目标页面URL模板，支持变量占位符"
    )
    render_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="页面渲染方式：StaticHTML/HeadlessBrowser"
    )
    field_extract_rules: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="字段提取规则配置（JSON格式）"
    )


class MetadataCrawlTask(Base):
    """爬取任务表"""

    __tablename__ = "metadata_crawl_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    object_name: Mapped[str] = mapped_column(String(500), nullable=False, comment="爬取对象名称")
    object_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="对象类型：Movie/Series/Cast"
    )
    source_id: Mapped[int] = mapped_column(
        ForeignKey("metadata_source.id", ondelete="SET NULL"),
        nullable=True, comment="数据源ID，FK → metadata_source.id"
    )
    source_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="数据源名称（冗余，数据源删除后保留）"
    )
    crawl_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="Created", server_default="Created",
        comment="爬取状态：Created/InProgress/Completed/Failed"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="失败原因"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="完成时间，仅Completed/Failed有值"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )


class MetadataCrawlDetail(Base):
    """爬取详情/候选值表"""

    __tablename__ = "metadata_crawl_detail"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("metadata_crawl_task.id", ondelete="CASCADE"),
        nullable=False, comment="爬取任务ID，FK → metadata_crawl_task.id"
    )
    field_name: Mapped[str] = mapped_column(String(200), nullable=False, comment="字段显示名称")
    field_code: Mapped[str] = mapped_column(String(200), nullable=False, comment="字段编码标识")
    crawl_data: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="爬取到的数据值"
    )
    is_used: Mapped[str] = mapped_column(
        String(10), nullable=False, default="NO", server_default="NO",
        comment="是否被用户选中：YES/NO"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
