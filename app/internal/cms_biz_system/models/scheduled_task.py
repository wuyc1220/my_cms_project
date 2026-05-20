"""
定时任务（ScheduledTask）及执行日志（ScheduledTaskLog）模型。

- ScheduledTask：定时任务定义表。每条记录代表一个具体任务，
  通过 task_type 唯一标识，代码层在 jobs 模块中提供其真实执行协程。
- ScheduledTaskLog：每次执行的日志，按 trigger_type 区分定时触发与人工触发。

状态值统一使用小写字符串，与前端契约保持一致：
- schedule_status: enabled / disabled
- execution_status: idle / running
- log.execution_status: success / failed / running
- trigger_type: scheduled / manual
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ScheduledTask(Base):
    __tablename__ = "scheduled_task"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_type: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, comment="任务类型唯一标识")
    description: Mapped[str | None] = mapped_column(String(255), nullable=True, comment="任务描述")
    cron_expression: Mapped[str] = mapped_column(String(50), nullable=False, comment="cron 表达式（5 或 6 位）")
    schedule_status: Mapped[str] = mapped_column(String(20), default="enabled", nullable=False, comment="enabled / disabled")
    execution_status: Mapped[str] = mapped_column(String(20), default="idle", nullable=False, comment="idle / running")
    execution_timeout: Mapped[int] = mapped_column(Integer, default=300, nullable=False, comment="执行超时时间（秒）")
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False, comment="失败重试次数")
    last_execution_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="上次执行时间")
    next_execution_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, comment="下次执行时间")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class ScheduledTaskLog(Base):
    __tablename__ = "scheduled_task_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[int] = mapped_column(
        ForeignKey("scheduled_task.id", ondelete="CASCADE"), nullable=False, index=True,
        comment="任务 ID，FK → scheduled_task.id",
    )
    execution_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False, comment="执行开始时间")
    trigger_type: Mapped[str] = mapped_column(String(20), nullable=False, comment="scheduled / manual")
    execution_status: Mapped[str] = mapped_column(String(20), nullable=False, comment="running / success / failed")
    duration: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True, comment="执行耗时（秒）")
    result: Mapped[str | None] = mapped_column(Text, nullable=True, comment="执行结果摘要或错误原因")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
