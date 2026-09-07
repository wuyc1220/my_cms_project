"""
内容（Content）Pydantic 数据模型。

覆盖交易管理视角的内容 CRUD、内容-许可证关联查询，以及点播管理（VOD）视角的列表展示。
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import app_tz


# ─── Content 列表项 / 详情响应 ────────────────────────────────────────

class LicensePlatformItem(BaseModel):
    """许可证平台项。"""
    platform: str
    ad_rights: bool

    model_config = ConfigDict(from_attributes=True)


class ContentLicenseRef(BaseModel):
    """内容关联的许可证简要信息。"""
    id: int
    name: str
    contract_id: Optional[int] = None
    contract_name: str
    provider_id: Optional[int] = None
    provider_name: str
    service_type: str
    service_type_name: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    platforms: Optional[list[LicensePlatformItem]] = None

    model_config = ConfigDict(from_attributes=True)


class ContentListItem(BaseModel):
    """内容列表项（交易视角）。"""
    id: int
    content_type: str
    title: str
    status: str
    external_id: Optional[str] = None
    parent_id: Optional[int] = None
    parent_title: Optional[str] = None
    genre_ids: Optional[list[int]] = None
    genre_name: Optional[str] = None
    custom_tag_ids: Optional[list[int]] = None
    custom_tag_names: Optional[list[str]] = None
    sequence: Optional[int] = None
    series_ordinal: Optional[int] = None
    volumn_count: Optional[int] = None
    begin_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    license_count: int = 0
    license_start: Optional[str] = None
    license_end: Optional[str] = None
    created_at: Optional[datetime] = None
    is_archived: Optional[bool] = None
    source_schedule_id: Optional[int] = None
    is_discarded: bool = False
    cutv_enable: Optional[bool] = None
    assignee_name: Optional[str] = None
    # 内容编排任务（arrangement）的开始/结束时间
    # 需求 3.6.6/3.6.7：子内容列表展示"对应的内容编排任务"的进展（区别于 SCHEDULE 专用的 begin_time/end_time）
    task_start_time: Optional[datetime] = None
    task_end_time: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

    @field_validator("begin_time", "end_time", "created_at", "task_start_time", "task_end_time", mode="after")
    @classmethod
    def _ensure_utc_on_read(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


# ─── SEASON 明细行（创建时的动态表格）────────────────────────────────

class SeasonDetailRow(BaseModel):
    """SEASON 创建时，每季的明细行：季号（自动填入）+ 该季集数。"""
    series_ordinal: int
    episode_count: int


# ─── 新建内容 ─────────────────────────────────────────────────────────

class ContentCreate(BaseModel):
    """新建内容请求体。动态字段随 content_type 变化。"""
    title: str = Field(..., max_length=100)
    content_type: str
    genre_ids: Optional[list[int]] = None
    custom_tag_ids: Optional[list[int]] = None
    parent_id: Optional[int] = None
    sequence: Optional[int] = None
    series_type: Optional[int] = None
    series_ordinal: Optional[int] = None
    volumn_count: Optional[int] = Field(None, ge=0, description="集/季数量，0表示空剧头")
    season_details: Optional[list[SeasonDetailRow]] = None
    begin_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    assignee_id: Optional[int] = Field(None, description="负责人ID（用于自动分配任务）")

    @field_validator("begin_time", "end_time", mode="after")
    @classmethod
    def _ensure_timezone(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=app_tz)
        return v


# ─── 编辑内容 ─────────────────────────────────────────────────────────

class ContentUpdate(BaseModel):
    """编辑内容请求体，所有字段均可选。"""
    title: Optional[str] = Field(None, max_length=100)
    genre_ids: Optional[list[int]] = None
    custom_tag_ids: Optional[list[int]] = None
    parent_id: Optional[int] = None
    sequence: Optional[int] = None
    begin_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    cutv_enable: Optional[bool] = None
    is_archived: Optional[bool] = None

    @field_validator("begin_time", "end_time", mode="after")
    @classmethod
    def _ensure_timezone(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=app_tz)
        return v


# ─── 内容简要信息（父级下拉选择）────────────────────────────────────

class ContentSimpleItem(BaseModel):
    """内容简要信息，用于 EPISODE 的父级 SERIES 下拉候选项等。"""
    id: int
    content_type: str
    title: str

    model_config = ConfigDict(from_attributes=True)


# ─── 任务指派人信息（详情页权限校验用）─────────────────────────────

class ContentTaskAssignees(BaseModel):
    """内容关联的任务指派人信息，用于详情页权限校验。"""
    arrangement_assignee_id: Optional[int] = None
    arrangement_assignee_name: Optional[str] = None
    arrangement_task_status: Optional[str] = None
    review_l1_assignee_id: Optional[int] = None
    review_l1_assignee_name: Optional[str] = None
    review_l1_task_status: Optional[str] = None
    review_l2_assignee_id: Optional[int] = None
    review_l2_assignee_name: Optional[str] = None
    review_l2_task_status: Optional[str] = None
    review_l3_assignee_id: Optional[int] = None
    review_l3_assignee_name: Optional[str] = None
    review_l3_task_status: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ─── 内容详情响应（含任务指派人信息）─────────────────────────────

class ContentDetailResponse(BaseModel):
    """内容详情响应，包含任务指派人信息（用于详情页权限校验）。"""
    content: ContentListItem
    task_assignees: ContentTaskAssignees

    model_config = ConfigDict(from_attributes=True)


# ─── 分页 ────────────────────────────────────────────────────────────

class ContentQueryParams(BaseModel):
    """内容列表查询参数（供 API 层使用）。"""
    page: int = 1
    page_size: int = 10
    external_id: Optional[str] = None
    title: Optional[str] = None
    content_types: Optional[list[str]] = None
    statuses: Optional[list[str]] = None
    genre_ids: Optional[list[int]] = None
    created_from: Optional[str] = None
    created_to: Optional[str] = None
    without_license: bool = False
    license_start_from: Optional[str] = None
    license_start_to: Optional[str] = None
    license_end_from: Optional[str] = None
    license_end_to: Optional[str] = None


# ─── VOD 内容列表项（点播管理视角）──────────────────────────────────

class VodContentListItem(BaseModel):
    """点播管理（VOD）内容列表项。"""
    id: int
    content_type: str
    title: str
    status: str
    genre_ids: Optional[list[int]] = None
    genre_name: Optional[str] = None
    type_name: Optional[str] = None
    category_name: Optional[str] = None
    custom_tag_names: list[str] = []
    unpublish_date: Optional[str] = None
    publish_date: Optional[str] = None
    poster_url: Optional[str] = None
    package_names: list[str] = []
    provider_names: list[str] = []
    license_start: Optional[str] = None
    license_end: Optional[str] = None
    created_at: Optional[datetime] = None
    is_discarded: bool = False

    model_config = ConfigDict(from_attributes=True)


class AdjacentContentResponse(BaseModel):
    """相邻内容 ID 响应。"""
    prev_id: Optional[int] = None
    next_id: Optional[int] = None


# ─── 批量导入 ─────────────────────────────────────────────────────────

class BatchImportItem(BaseModel):
    """批量导入的每条内容。"""
    title: str = Field(..., max_length=100)
    content_type: str = Field(..., pattern="^(EPISODE|SERIES|SEASON_SERIES)$")
    series_ordinal: Optional[int] = None
    sequence: Optional[int] = None
    series_type: Optional[int] = None
    assignee_id: Optional[int] = None


class BatchImportRequest(BaseModel):
    """批量导入请求体。"""
    parent_id: int
    items: list[BatchImportItem]


class BatchImportResultItem(BaseModel):
    """批量导入单条结果。"""
    title: str
    content_id: Optional[int] = None
    success: bool
    error: Optional[str] = None


class BatchImportResponse(BaseModel):
    """批量导入响应。"""
    success_count: int
    failed_count: int
    details: list[BatchImportResultItem]
