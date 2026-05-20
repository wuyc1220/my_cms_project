"""
任务管理 - Pydantic 请求/响应模型。
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict


# ─── 列表项 ────────────────────────────────────────────────────────────

class TaskListItem(BaseModel):
    id: int
    content_id: int
    content_name: str
    content_type: str
    ingest_status: str = "None"
    task_type: str
    assignee_id: int | None = None
    assignee_name: str | None = None
    task_status: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    created_at: datetime | None = None
    has_children: bool = False

    model_config = ConfigDict(from_attributes=True)


# ─── 详情 ──────────────────────────────────────────────────────────────

class TaskDetail(BaseModel):
    id: int
    content_id: int
    content_name: str
    content_type: str
    task_type: str
    assignee_id: int | None = None
    assignee_name: str | None = None
    task_status: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    has_children: bool = False

    model_config = ConfigDict(from_attributes=True)


# ─── 操作历史 ──────────────────────────────────────────────────────────

class TaskHistoryItem(BaseModel):
    id: int
    task_id: int
    processed_type: str
    processed_by: str | None = None
    processed_at: datetime
    previous_value: str | None = None
    updated_value: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ─── 请求体 ────────────────────────────────────────────────────────────

class TaskAssignRequest(BaseModel):
    assignee_id: int
    update_childs: bool = False


class BatchAssignRequest(BaseModel):
    task_ids: list[int]
    assignee_id: int
    update_childs: bool = False


# ─── 查询参数（由 API 层 Query 参数构造）────────────────────────────────

class TaskQueryParams(BaseModel):
    page: int = 1
    page_size: int = 10
    task_types: list[str] | None = None
    task_statuses: list[str] | None = None
    assignee_keyword: str | None = None
    assignee_id: int | None = None
    assignee_is_null: bool = False  # 查询未分配的任务
    content_name: str | None = None
    content_types: list[str] | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    end_time_start: datetime | None = None
    end_time_end: datetime | None = None
    sort_by: str | None = None
    sort_order: str | None = None
