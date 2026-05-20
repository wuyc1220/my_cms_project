"""
元数据增强模块 - Pydantic 请求/响应模型
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ═════════════════════════════════════════════════════════════
# 数据源 MetadataSource
# ═════════════════════════════════════════════════════════════

class MetadataSourceCreate(BaseModel):
    """新增数据源"""
    name: str = Field(..., max_length=200)
    content_type: str = Field(..., max_length=50, description="Movie/Series/Cast")
    collect_type: str = Field(default="API", max_length=50, description="采集方式：API/网页爬取")
    url: str = Field(..., max_length=500)
    api_endpoint: str | None = Field(None, max_length=500)
    auth_type: str | None = Field(None, max_length=50, description="API_Key/OAuth2.0/None")
    api_key: str | None = Field(None, max_length=500, description="认证方式为API_Key时必填")
    rate_limit: int = Field(default=1000, ge=1, description="请求频率限制（次/小时）")
    status: str = Field(default="YES", max_length=10, description="YES/NO")
    page_url_template: str | None = Field(None, max_length=500, description="爬取目标页面URL模板")
    render_type: str | None = Field(None, max_length=50, description="StaticHTML/HeadlessBrowser")
    field_extract_rules: str | None = Field(None, description="字段提取规则配置（JSON格式）")


class MetadataSourceUpdate(BaseModel):
    """编辑数据源"""
    name: str | None = Field(None, max_length=200)
    content_type: str | None = Field(None, max_length=50)
    collect_type: str | None = Field(None, max_length=50)
    url: str | None = Field(None, max_length=500)
    api_endpoint: str | None = Field(None, max_length=500)
    auth_type: str | None = Field(None, max_length=50)
    api_key: str | None = Field(None, max_length=500)
    rate_limit: int | None = Field(None, ge=1)
    status: str | None = Field(None, max_length=10)
    page_url_template: str | None = Field(None, max_length=500)
    render_type: str | None = Field(None, max_length=50)
    field_extract_rules: str | None = Field(None)


class MetadataSourceListItem(BaseModel):
    """数据源列表项"""
    id: int
    name: str
    content_type: str
    collect_type: str
    url: str
    api_endpoint: str | None = None
    auth_type: str | None = None
    api_key: str | None = Field(None, description="前端展示为掩码")
    rate_limit: int
    status: str
    page_url_template: str | None = None
    render_type: str | None = None
    field_extract_rules: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class MetadataSourceStatusToggle(BaseModel):
    """切换数据源状态"""
    status: str = Field(..., max_length=10, description="YES/NO")


class BatchStatusRequest(BaseModel):
    """批量启用/禁用请求"""
    ids: list[int]
    status: str = Field(..., max_length=10, description="YES/NO")


class MetadataSourceQueryParams(BaseModel):
    """数据源查询参数"""
    page: int = 1
    page_size: int = 10
    name: str | None = None
    content_types: list[str] | None = None
    collect_types: list[str] | None = None
    statuses: list[str] | None = None


# ═════════════════════════════════════════════════════════════
# 爬取任务 MetadataCrawlTask
# ═════════════════════════════════════════════════════════════

class MetadataCrawlTaskListItem(BaseModel):
    """爬取任务列表项"""
    id: int
    object_name: str
    object_type: str
    source_id: int | None = None
    source_name: str | None = None
    crawl_status: str
    created_at: datetime | None = None
    completed_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class MetadataCrawlDetailItem(BaseModel):
    """爬取详情/候选值"""
    id: int
    task_id: int
    field_name: str
    field_code: str
    crawl_data: str | None = None
    is_used: str = "NO"
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class MetadataCrawlTaskDetail(BaseModel):
    """爬取任务详情"""
    id: int
    object_name: str
    object_type: str
    source_id: int | None = None
    source_name: str | None = None
    crawl_status: str
    error_message: str | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    details: list[MetadataCrawlDetailItem] = []

    model_config = ConfigDict(from_attributes=True)


class CrawlTaskQueryParams(BaseModel):
    """爬取任务查询参数"""
    page: int = 1
    page_size: int = 10
    source_name: str | None = None
    object_name: str | None = None
    object_types: list[str] | None = None
    crawl_statuses: list[str] | None = None


# ═════════════════════════════════════════════════════════════
# 爬取请求与结果
# ═════════════════════════════════════════════════════════════

class CrawlRequest(BaseModel):
    """触发爬取请求"""
    object_name: str = Field(..., max_length=500, description="爬取对象名称")
    object_type: str = Field(..., max_length=50, description="对象类型：Movie/Series/Cast")
    field_codes: list[dict[str, str]] = Field(
        ..., description="字段列表：[{code, name}]，code为字段编码，name为字段显示名"
    )


class CrawlFieldCandidate(BaseModel):
    """单个字段的候选值"""
    detail_id: int
    field_code: str
    field_name: str
    crawl_data: str | None = None
    source_name: str | None = None


class CrawlProgressItem(BaseModel):
    """爬取进度项"""
    task_id: int
    source_name: str
    crawl_status: str
    progress: int = Field(default=0, ge=0, le=100, description="进度百分比")


class CrawlResponse(BaseModel):
    """爬取响应（进度+候选值）"""
    object_name: str
    object_type: str
    progress_items: list[CrawlProgressItem] = []
    field_candidates: dict[str, list[CrawlFieldCandidate]] = {}


class CrawlConfirmSelection(BaseModel):
    """单个字段的选择"""
    detail_id: int
    is_used: str = Field(default="YES", max_length=10, description="YES/NO")


class CrawlConfirmRequest(BaseModel):
    """确认选择请求"""
    selections: list[CrawlConfirmSelection]


class CrawlConfirmResult(BaseModel):
    """确认选择结果"""
    field_code: str
    field_name: str
    crawl_data: str | None = None
