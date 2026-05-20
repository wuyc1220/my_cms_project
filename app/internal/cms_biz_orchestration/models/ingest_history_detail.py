"""
注入历史明细（Ingest History Detail）领域模型。

记录每次发布中涉及的关联对象（Cast/Category/Package/Picture/Movie/CastRoleMap）的动作明细。
主记录在 ingest_history 表中（Content 级别），此表为明细补充。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IngestHistoryDetail(Base):
    """
    注入历史明细表。

    每次发布 Content 时，为涉及的关联对象创建明细记录。
    SKIP 的对象不创建明细（无变更则不记录）。
    """

    __tablename__ = "ingest_history_detail"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    history_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("ingest_history.id", ondelete="CASCADE"), nullable=False, comment="关联的IngestHistory主记录ID"
    )
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="实体类型: Cast/Category/Package/Picture/Movie/CastRoleMap"
    )
    entity_id: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="实体ID"
    )
    entity_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="实体名称"
    )
    action: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="操作类型: REGIST/UPDATE"
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
        Integer, nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="更新人ID"
    )
