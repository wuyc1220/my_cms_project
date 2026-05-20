"""
服务包（Package）Pydantic 数据模型。

包含：
- PackageListItem    列表/详情响应
- PackageCreate      新建请求体
- PackageUpdate      编辑请求体
- ContentSimpleItem  服务包关联内容的简要信息
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ─── Content（内容简要信息，用于"添加内容至服务包"弹框）──────────────

class ContentSimpleItem(BaseModel):
    """
    内容简要信息。

    输出字段：
        id              内容 id
        content_type    内容类型（MOVIE/EPISODE/SERIES/SEASON/CHANNEL/SCHEDULE）
        title           内容标题
        genre           题材名称
        custom_tags     自定义标签列表
        status          Ingest 状态
        has_license     是否已关联有效许可证（占位，待许可证模块实现后填充真实数据）
    """

    id: int
    content_type: str
    title: str
    genre: str | None = None
    custom_tags: list[str] = []
    status: str
    has_license: bool = False

    model_config = ConfigDict(from_attributes=True)


# ─── Package（服务包）────────────────────────────────────────────────

class PackageListItem(BaseModel):
    """
    服务包列表项 / 详情响应。

    输出字段：
        id              服务包 id
        name            服务包名称
        package_type    服务包类型（来源数据字典 Package_Type）
        platforms       平台列表（来源数据字典 Platform）
        description     描述
        ingest_status   Ingest 状态（success/failure）
        created_at      创建时间
    """

    id: int
    name: str
    package_type: str | None
    platforms: list[str] = []
    description: str | None = None
    ingest_status: str = "failure"
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class PackageCreate(BaseModel):
    """
    新建服务包请求体。

    必填：name
    选填：package_type、platforms、description
    """

    name: str = Field(..., max_length=100)
    package_type: str | None = None
    platforms: list[str] = []
    description: str | None = Field(None, max_length=500)


class PackageUpdate(BaseModel):
    """
    编辑服务包请求体，所有字段均可选，仅传递需要更新的字段。
    """

    name: str | None = Field(None, max_length=100)
    package_type: str | None = None
    platforms: list[str] | None = None
    description: str | None = Field(None, max_length=500)
    ingest_status: str | None = None


class PackageContentAddRequest(BaseModel):
    """
    向服务包添加内容的请求体。

    字段：
        content_ids     要关联的内容 id 列表（支持批量添加）
    """

    content_ids: list[int]
