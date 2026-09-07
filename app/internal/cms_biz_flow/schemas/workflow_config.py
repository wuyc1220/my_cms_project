"""
工作流配置 Pydantic 数据模型。

覆盖：
- 流程配置列表/详情
- 流程节点配置
- 流程编排
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ─── 流程节点配置 ────────────────────────────────────────────────────────

class WorkflowNodeConfigItem(BaseModel):
    """
    流程节点配置项。

    字段：
        id                节点ID
        node_code         节点编码
        node_name         节点显示名称
        node_type         节点类型: process/parallel_box
        mandatory         是否必须环节
        parallel_rule     并行聚合规则: any_required/all_required
        bind_status_before 绑定前置状态
        bind_status_after  绑定后置状态
        position_x        画布X坐标
        position_y        画布Y坐标
        width             节点宽度
        height            节点高度
        sequence          顺序号
        parent_node_id    父节点ID
    """
    id: int
    node_code: str
    node_name: str
    node_type: str
    mandatory: bool = True
    parallel_rule: Optional[str] = None
    bind_status_before: Optional[str] = None
    bind_status_after: Optional[str] = None
    position_x: Optional[int] = None
    position_y: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    sequence: int = 0
    parent_node_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowNodeConfigCreate(BaseModel):
    """创建流程节点配置请求。"""
    node_code: str = Field(..., max_length=100)
    node_name: str = Field(..., max_length=100)
    node_type: str = "process"
    mandatory: bool = True
    parallel_rule: Optional[str] = None
    bind_status_before: Optional[str] = None
    bind_status_after: Optional[str] = None
    position_x: Optional[int] = None
    position_y: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    sequence: int = 0
    parent_node_id: Optional[int] = None


class WorkflowNodeConfigUpdate(BaseModel):
    """更新流程节点配置请求。"""
    node_name: Optional[str] = Field(None, max_length=100)
    mandatory: Optional[bool] = None
    parallel_rule: Optional[str] = None
    bind_status_before: Optional[str] = None
    bind_status_after: Optional[str] = None
    position_x: Optional[int] = None
    position_y: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    sequence: Optional[int] = None


# ─── 流程配置 ─────────────────────────────────────────────────────────

class WorkflowConfigListItem(BaseModel):
    """
    流程配置列表项。

    字段：
        id                配置ID
        process_code      流程编码
        process_name      流程名称
        belonging         所属模块
        status            状态: draft/published
        version           版本号
        published_version 已发布版本号
        updated_at        更新时间
        published_at      发布时间
    """
    id: int
    process_code: str
    process_name: str
    belonging: str
    status: str
    version: int
    published_version: Optional[int] = None
    updated_at: Optional[datetime] = None
    published_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowConfigDetail(BaseModel):
    """
    流程配置详情（含节点）。

    字段：
        id                配置ID
        process_code      流程编码
        process_name      流程名称
        belonging         所属模块
        status            状态
        version           版本号
        published_version 已发布版本号
        config_json       流程编排JSON
        nodes             节点列表
        created_at        创建时间
        updated_at        更新时间
    """
    id: int
    process_code: str
    process_name: str
    belonging: str
    status: str
    version: int
    published_version: Optional[int] = None
    config_json: Optional[str] = None
    nodes: list[WorkflowNodeConfigItem] = []
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class WorkflowConfigCreate(BaseModel):
    """创建流程配置请求。"""
    process_code: str = Field(..., max_length=100)
    process_name: str = Field(..., max_length=100)
    belonging: str  # MOVIE/EPISODE/SEASON/SEASON_SERIES/SERIES/CHANNEL/SCHEDULE


class WorkflowConfigUpdate(BaseModel):
    """更新流程配置请求。"""
    process_code: Optional[str] = Field(None, max_length=100)
    process_name: Optional[str] = Field(None, max_length=100)
    belonging: Optional[str] = None
    config_json: Optional[str] = None


class WorkflowConfigPublishRequest(BaseModel):
    """发布流程配置请求。"""
    pass


# ─── 流程编排 ─────────────────────────────────────────────────────────

class WorkflowEdge(BaseModel):
    """流程连线。"""
    from_node: str = Field(..., alias="from")
    to_node: str = Field(..., alias="to")


class WorkflowConfigJson(BaseModel):
    """流程编排JSON结构。"""
    nodes: list[dict] = []
    edges: list[WorkflowEdge] = []


# ─── 内容流程实例（扩展） ─────────────────────────────────────────────────

class ContentProcessInstanceItem(BaseModel):
    """
    内容流程实例项（扩展原有 ProcessListItem）。

    字段：
        id                  实例ID
        content_id          内容ID
        workflow_config_id  流程配置ID
        node_config_id      节点配置ID
        name                节点名称
        node_code           节点编码
        sequence            顺序号
        status              状态: Pending/Running/Passed/Failed/Skipped
        start_dt            开始时间
        end_dt              结束时间
        mandatory           是否必须
        bind_status_before  绑定前置状态
        bind_status_after   绑定后置状态
        parent_node_id      父节点ID
    """
    id: int
    content_id: int
    workflow_config_id: Optional[int] = None
    node_config_id: Optional[int] = None
    name: str
    node_code: Optional[str] = None
    sequence: int
    status: str
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    mandatory: bool = True
    bind_status_before: Optional[str] = None
    bind_status_after: Optional[str] = None
    parent_node_id: Optional[int] = None

    model_config = ConfigDict(from_attributes=True)
