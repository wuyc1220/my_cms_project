"""
看板统计模块数据模型。

包含用户看板配置表，用于存储用户自定义的看板布局设置。
"""

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class UserDashboardConfig(Base):
    """
    用户看板配置表。

    存储用户自定义的看板布局、显示模块顺序、内容状态筛选、题材筛选等配置。
    """

    __tablename__ = "user_dashboard_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("cms_user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="用户ID",
    )

    # 模块配置：存储模块显示状态和排序
    # 格式: [{"code": "published_stats", "name": "...", "visible": true, "sort_order": 1}, ...]
    module_config: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="模块配置列表",
    )

    # 内容状态配置：存储选中的内容状态
    # 格式: [{"code": "WaitingForMaterials", "name": "...", "visible": true, "sort_order": 1}, ...]
    content_status_config: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="内容状态配置列表",
    )

    # 题材配置：存储选中的题材
    # 格式: [{"id": 1, "name": "...", "visible": true, "sort_order": 1}, ...]
    content_genre_config: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default="[]",
        comment="题材配置列表",
    )

    # 审计字段
    is_deleted: Mapped[bool] = mapped_column(
        default=False,
        server_default="false",
        nullable=False,
        comment="软删除标记",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间",
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
