"""
发布管理（Publish）Pydantic 数据模型。

包含发布任务列表、发布计划设置、注入历史查询等 Schema。
"""
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import app_tz


# ═══════════════════════════════════════════════════════════
# 发布任务列表
# ═══════════════════════════════════════════════════════════

class PublishListItem(BaseModel):
    """发布管理列表项"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    entity_name: Optional[str] = None
    content_type: Optional[str] = None
    ingest_status: Optional[str] = None  # 从关联实体获取
    publish_status: str
    task_type: Optional[str] = None  # 任务类型: publish/unpublish
    publish_time: Optional[datetime] = None
    unpublish_time: Optional[datetime] = None
    scheduled_time: Optional[datetime] = None
    execution_mode: Optional[str] = None


class PublishQueryParams(BaseModel):
    """发布列表查询参数"""
    page: int = 1
    page_size: int = 10
    content_name: Optional[str] = None
    content_types: Optional[list[str]] = None
    ingest_statuses: Optional[list[str]] = None
    publish_statuses: Optional[list[str]] = None
    publish_time_from: Optional[str] = None
    publish_time_to: Optional[str] = None
    unpublish_time_from: Optional[str] = None
    unpublish_time_to: Optional[str] = None


# ═══════════════════════════════════════════════════════════
# 发布/下架计划设置
# ═══════════════════════════════════════════════════════════

class PublishPlanCreate(BaseModel):
    """设置发布/下架计划请求"""
    entity_type: Optional[str] = Field(None, description="实体类型: Content/Channel/Schedule")
    entity_id: Optional[int] = Field(None, description="实体ID")
    entity_name: Optional[str] = Field(None, description="实体名称")
    content_type: Optional[str] = Field(None, description="内容类型")
    task_type: str = Field("publish", description="任务类型: publish/unpublish")
    execution_mode: str = Field(..., description="执行方式: now/plan")
    scheduled_time: Optional[datetime] = Field(None, description="计划执行时间")

    @field_validator("scheduled_time", mode="before")
    @classmethod
    def _parse_datetime(cls, v):
        if isinstance(v, str):
            v = v.replace(" ", "T")
        return v

    @field_validator("scheduled_time", mode="after")
    @classmethod
    def _ensure_timezone(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=app_tz)
        return v

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "entity_type": "Content",
                    "entity_id": 1,
                    "entity_name": "测试内容",
                    "content_type": "MOVIE",
                    "task_type": "publish",
                    "execution_mode": "plan",
                    "scheduled_time": "2024-01-15T10:30:00"
                }
            ]
        }
    }


class PublishPlanUpdate(BaseModel):
    """修改发布/下架计划请求"""
    execution_mode: str = Field(..., description="执行方式: now/plan")
    scheduled_time: Optional[datetime] = Field(None, description="计划执行时间")

    @field_validator("scheduled_time", mode="before")
    @classmethod
    def _parse_datetime(cls, v):
        if isinstance(v, str):
            v = v.replace(" ", "T")
        return v

    @field_validator("scheduled_time", mode="after")
    @classmethod
    def _ensure_timezone(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=app_tz)
        return v


class PublishPlanResponse(BaseModel):
    """发布计划响应"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    entity_name: Optional[str] = None
    content_type: Optional[str] = None
    task_type: str
    execution_mode: str
    scheduled_time: Optional[datetime] = None
    status: str
    publish_status: str
    correlate_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime


# ═══════════════════════════════════════════════════════════
# 批量操作
# ═══════════════════════════════════════════════════════════

class BatchPublishRequest(BaseModel):
    """批量发布/下架请求"""
    entity_ids: list[int] = Field(..., description="实体ID列表")
    entity_type: str = Field(..., description="实体类型")
    task_type: str = Field(..., description="任务类型: publish/unpublish")
    execution_mode: str = Field(..., description="执行方式: now/plan")
    scheduled_time: Optional[datetime] = Field(None, description="计划执行时间")

    @field_validator("scheduled_time", mode="before")
    @classmethod
    def _parse_datetime(cls, v):
        if isinstance(v, str):
            v = v.replace(" ", "T")
        return v


# ═══════════════════════════════════════════════════════════
# 注入历史
# ═══════════════════════════════════════════════════════════

class IngestHistoryItem(BaseModel):
    """注入历史列表项"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    entity_id: int
    entity_name: Optional[str] = None
    action: str  # REGIST / UPDATE / DELETE
    status: str  # success / failure
    create_date: datetime
    send_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    ingest_xml_path: Optional[str] = None
    result_xml_path: Optional[str] = None
    ingest_xml_url: Optional[str] = None
    result_xml_url: Optional[str] = None


class IngestHistoryQueryParams(BaseModel):
    """注入历史查询参数"""
    page: int = 1
    page_size: int = 10
    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    action: Optional[str] = None
    status: Optional[str] = None
