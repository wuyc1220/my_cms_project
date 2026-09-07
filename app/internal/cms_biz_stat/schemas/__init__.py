"""
看板统计模块 Pydantic Schema。

包含看板数据响应模型和用户配置模型。
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


# ═══════════════════════════════════════════════════════════
# 1. 基础数据项 Schema
# ═══════════════════════════════════════════════════════════


class PieDataItem(BaseModel):
    """饼图数据项"""
    name: str
    value: int


class StatusCountItem(BaseModel):
    """状态统计项"""
    status_code: str
    status_name: str
    count: int


# ═══════════════════════════════════════════════════════════
# 2. 内容统计 Schema
# ═══════════════════════════════════════════════════════════


class PublishedStatsResponse(BaseModel):
    """内容发布统计响应"""
    by_platform: list[PieDataItem]
    by_content_type: list[PieDataItem]
    by_genre: list[PieDataItem]
    by_ingest_status: list[PieDataItem]


class ContentStatusCountResponse(BaseModel):
    """内容状态统计响应（Ingest_Status 字典状态 + Near-Expired/Deleted 计算状态）"""
    waiting_for_materials: int
    in_progress: int
    ready_for_publish: int
    publishing: int
    published: int
    publish_failed: int
    no_active_license: int
    closed: int
    none_status: int
    # 计算状态（仅 Content Status Count 模块展示）
    near_expired: int = 0
    near_expiry_days: int = 7
    deleted: int = 0


class GenreStatusMatrixResponse(BaseModel):
    """题材x状态矩阵响应"""
    genres: list[str]
    statuses: list[str]
    data: dict[str, dict[str, int]]


# ═══════════════════════════════════════════════════════════
# 3. 任务统计 Schema
# ═══════════════════════════════════════════════════════════


class TaskCompletionStatsResponse(BaseModel):
    """任务完成统计响应"""
    arrangement: list[PieDataItem]
    review_l1: list[PieDataItem]
    review_l2: list[PieDataItem]
    review_l3: list[PieDataItem]


class TaskStatusCountResponse(BaseModel):
    """任务状态数量统计响应"""
    pending: int
    completed: int
    not_assigned: int


class TaskAssignedMatrixItem(BaseModel):
    """任务分配矩阵行项"""
    user_id: int
    user_name: str
    arrangement_pending: int
    review_l1_pending: int
    review_l2_pending: int
    review_l3_pending: int
    arrangement_completed: int
    review_completed: int
    completion_rate: float


class TaskAssignedMatrixResponse(BaseModel):
    """任务分配矩阵响应"""
    users: list[str]
    data: list[TaskAssignedMatrixItem]


# ═══════════════════════════════════════════════════════════
# 4. 用户配置 Schema
# ═══════════════════════════════════════════════════════════


class ModuleConfigItem(BaseModel):
    """模块配置项"""
    code: str
    name: str
    visible: bool
    sort_order: int


class StatusConfigItem(BaseModel):
    """状态配置项"""
    code: str
    name: str
    visible: bool
    sort_order: int


class GenreConfigItem(BaseModel):
    """题材配置项"""
    id: int
    name: str
    visible: bool
    sort_order: int


class UserDashboardConfigResponse(BaseModel):
    """用户看板配置响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    module_config: list[ModuleConfigItem]
    content_status_config: list[StatusConfigItem]
    content_genre_config: list[GenreConfigItem]


class UserDashboardConfigUpdate(BaseModel):
    """用户看板配置更新请求"""
    module_config: list[ModuleConfigItem]
    content_status_config: list[StatusConfigItem]
    content_genre_config: list[GenreConfigItem]


# ═══════════════════════════════════════════════════════════
# 5. 看板综合数据 Schema
# ═══════════════════════════════════════════════════════════


class DashboardDataResponse(BaseModel):
    """看板综合数据响应"""
    published_stats: PublishedStatsResponse
    content_status_count: ContentStatusCountResponse
    genre_status_matrix: GenreStatusMatrixResponse
    task_completion_stats: TaskCompletionStatsResponse
    task_status_count: TaskStatusCountResponse
    task_assigned_matrix: TaskAssignedMatrixResponse
    # 当前用户是否可见任务相关模块（后端实时查询权限，作为前端显隐权威来源）
    can_see_task_modules: bool = False
