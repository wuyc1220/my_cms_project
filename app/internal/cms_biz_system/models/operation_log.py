from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class OperationLog(Base):
    __tablename__ = "operation_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    operation_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    operation_object: Mapped[str | None] = mapped_column(String(200), nullable=True)
    operation_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    previous_value: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="操作前的实体快照（JSON 字符串）",
    )
    updated_value: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="操作后的实体快照（JSON 字符串）",
    )
    updated_value_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="操作后的原始 JSON（未经转译，保留原始 ID/Code）",
    )
    content_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="关联内容 ID，供 Activity Log 精确查询",
    )
    entity_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True,
        comment="实体类型（license/contract/provider/content 等），供详情页历史精确查询",
    )
    entity_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, index=True,
        comment="实体 ID，与 entity_type 配合精确查询",
    )
    operation_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip_address: Mapped[str | None] = mapped_column(String(50), nullable=True)
    result: Mapped[str] = mapped_column(String(20), default="success")  # success / failed
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
