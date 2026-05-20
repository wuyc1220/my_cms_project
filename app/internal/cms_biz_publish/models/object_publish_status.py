"""
对象发布状态跟踪模型。

用于跟踪每个对象（Content/Cast/Package/Picture/Movie/Category）的发布状态，
准确判断C2规范的Action（REGIST/UPDATE/DELETE）。

设计原则：
1. 主对象（Program/Series/Channel/Schedule）支持完整的发布/下架生命周期
2. 关联对象（Cast/Package/Picture/Movie/Category）只有发布状态，无下架概念
3. 记录首次发布时间和最后发布时间，用于统计和审计
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class ObjectPublishStatus(Base):
    """
    对象发布状态跟踪表。
    
    字段：
        id                  主键
        entity_type         实体类型: Content/Cast/Package/Picture/Movie/Category
        entity_id           实体ID
        content_id          关联的主内容ID（Program/Series/Channel/Schedule）
        is_published        是否曾经发布成功过（核心字段，用于Action判断）
        last_action         最后一次动作: REGIST/UPDATE/DELETE
        first_publish_time  首次发布时间
        last_publish_time   最后发布时间
        last_unpublish_time 最后下架时间（仅主对象使用）
        created_at          创建时间
        updated_at          更新时间
    """
    
    __tablename__ = "object_publish_status"
    
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(
        String(50), 
        nullable=False, 
        comment="实体类型: Content/Cast/Package/Picture/Movie/Category"
    )
    entity_id: Mapped[int] = mapped_column(
        Integer, 
        nullable=False, 
        comment="实体ID"
    )
    content_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("content.id", ondelete="SET NULL"),
        nullable=True,
        comment="关联的主内容ID（Program/Series/Channel/Schedule）"
    )
    is_published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="是否曾经发布成功过"
    )
    last_action: Mapped[Optional[str]] = mapped_column(
        String(20),
        nullable=True,
        comment="最后一次动作: REGIST/UPDATE/DELETE"
    )
    first_publish_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="首次发布时间"
    )
    last_publish_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最后发布时间"
    )
    last_unpublish_time: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="最后下架时间（仅主对象使用）"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
        comment="更新时间"
    )
    
    # 关系（可选，用于查询）
    content: Mapped[Optional["Content"]] = relationship(
        "Content",
        lazy="select",
        foreign_keys=[content_id]
    )
    
    def __repr__(self) -> str:
        return (
            f"<ObjectPublishStatus(id={self.id}, entity_type='{self.entity_type}', "
            f"entity_id={self.entity_id}, is_published={self.is_published})>"
        )
    
    # ═══════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════
    
    def mark_as_published(self, action: str) -> None:
        """
        标记为已发布成功。
        
        Args:
            action: REGIST 或 UPDATE
        """
        self.is_published = True
        self.last_action = action

        now = datetime.now(timezone.utc)
        if not self.first_publish_time:
            self.first_publish_time = now
        self.last_publish_time = now

        # 如果是重新上架（之前下架过），清空下架时间
        if self.last_unpublish_time:
            self.last_unpublish_time = None
    
    def mark_as_unpublished(self) -> None:
        """标记为已下架（仅主对象使用）"""
        self.last_action = "DELETE"
        self.last_unpublish_time = datetime.now(timezone.utc)
        # 下架后LSP端对象已删除，is_published重置为False
        # 这样重新上架时才能正确判断为REGIST
        self.is_published = False
    
    def mark_as_failed(self) -> None:
        """标记为发布失败"""
        # 不改变 is_published 状态，只记录最后动作
        self.last_action = self.last_action or "REGIST"  # 保持原动作或默认REGIST
