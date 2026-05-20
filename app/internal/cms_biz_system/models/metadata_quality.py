"""
元数据质量检查记录及问题明细模型。

- MetadataQualityCheck：每次质量检查的汇总（一条 = 一次任务执行），
  包括状态、总数 / 通过 / 不通过、耗时。支持软删除（批量删除质检记录）。
- MetadataQualityIssue：本次检查发现的问题明细。
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MetadataQualityCheck(Base):
    __tablename__ = "metadata_quality_check"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="检查开始时间")
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, comment="pending / running / completed / failed")
    total_contents: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="检查内容总数")
    passed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="通过数量")
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="未通过数量")
    duration: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True, comment="检查耗时（秒），Running 状态为 NULL")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False, comment="软删除标记")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class MetadataQualityIssue(Base):
    __tablename__ = "metadata_quality_issue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    check_id: Mapped[int] = mapped_column(
        ForeignKey("metadata_quality_check.id", ondelete="CASCADE"), nullable=False, index=True,
        comment="所属检查记录 ID，FK → metadata_quality_check.id",
    )
    content_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="内容 ID")
    content_name: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="内容名称")
    content_type: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="内容类型 MOVIE / SERIES ...")
    issue_type: Mapped[str] = mapped_column(String(30), nullable=False, comment="missing / format / invalid")
    field_name: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="问题字段")
    severity: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="critical / medium / minor")
    expected_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="期望值")
    actual_value: Mapped[str | None] = mapped_column(Text, nullable=True, comment="实际值")
