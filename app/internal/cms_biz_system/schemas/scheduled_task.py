"""定时任务（ScheduledTask）相关 Pydantic schemas。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ScheduledTaskOut(BaseModel):
    """前端列表/详情基础字段。与前端契约一致，将 ORM 的 updated_at 映射为 modified_at。"""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    task_type: str
    description: str | None = None
    schedule_status: Literal["enabled", "disabled"]
    execution_status: Literal["idle", "running"]
    cron_expression: str
    last_execution_time: datetime | None = None
    next_execution_time: datetime | None = None
    created_at: datetime
    modified_at: datetime = Field(validation_alias="updated_at")
    execution_timeout: int
    retry_count: int


class ScheduledTaskLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    execution_time: datetime
    trigger_type: Literal["scheduled", "manual"]
    execution_status: Literal["success", "failed", "running"]
    duration: float | None = None
    result: str | None = ""


class ScheduledTaskDetail(ScheduledTaskOut):
    execution_logs: list[ScheduledTaskLogOut] = []


class TriggerScheduledTasksRequest(BaseModel):
    ids: list[int]


class TriggerScheduledTasksResponse(BaseModel):
    success: bool = True
    triggered: int


class UpdateCronRequest(BaseModel):
    """更新定时任务的 Cron 表达式。"""
    cron_expression: str = Field(..., min_length=1, max_length=100, description="Cron 表达式（5 或 6 位）")
