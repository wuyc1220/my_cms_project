"""元数据校验规则（MetadataValidationRule）相关 Pydantic schemas。"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class MetadataValidationRuleOut(BaseModel):
    """校验规则响应（列表项）。"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    entity_type: str
    field_name: str
    rule_type: str
    is_mandatory: str
    mandatory_condition: Optional[str] = None
    max_length: Optional[int] = None
    min_length: Optional[int] = None
    allowed_values: Optional[str] = None
    regex_pattern: Optional[str] = None
    data_type: Optional[str] = None
    multi_language: bool
    rule_config: Optional[dict] = None
    severity: str
    is_enabled: bool
    is_system: bool
    description: Optional[str] = None
    example_value: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class MetadataValidationRuleDetail(MetadataValidationRuleOut):
    """校验规则详情（包含审计字段）。"""
    created_by: Optional[int] = None
    updated_by: Optional[int] = None


class MetadataValidationRuleCreate(BaseModel):
    """创建校验规则请求。"""
    entity_type: str = Field(..., max_length=50, description="实体类型")
    field_name: str = Field(..., max_length=100, description="字段名称")
    rule_type: str = Field(..., max_length=50, description="规则类型")
    is_mandatory: str = Field("N", max_length=1, description="必填标识: Y/N/C")
    mandatory_condition: Optional[str] = Field(None, description="条件必填触发条件（JSON）")
    max_length: Optional[int] = Field(None, ge=0, description="最大长度")
    min_length: Optional[int] = Field(None, ge=0, description="最小长度")
    allowed_values: Optional[str] = Field(None, description="允许值列表")
    regex_pattern: Optional[str] = Field(None, max_length=500, description="正则表达式")
    data_type: Optional[str] = Field(None, max_length=50, description="数据类型")
    multi_language: bool = Field(False, description="是否支持多语言")
    rule_config: Optional[dict] = Field(None, description="扩展配置（JSONB）")
    severity: str = Field("medium", max_length=20, description="严重级别")
    is_enabled: bool = Field(True, description="是否启用")
    is_system: bool = Field(True, description="是否系统内置")
    description: Optional[str] = Field(None, description="规则说明")
    example_value: Optional[str] = Field(None, description="示例值")


class MetadataValidationRuleUpdate(BaseModel):
    """更新校验规则请求。"""
    is_mandatory: Optional[str] = Field(None, max_length=1, description="必填标识")
    mandatory_condition: Optional[str] = Field(None, description="条件必填触发条件")
    max_length: Optional[int] = Field(None, ge=0, description="最大长度")
    min_length: Optional[int] = Field(None, ge=0, description="最小长度")
    allowed_values: Optional[str] = Field(None, description="允许值列表")
    regex_pattern: Optional[str] = Field(None, max_length=500, description="正则表达式")
    data_type: Optional[str] = Field(None, max_length=50, description="数据类型")
    multi_language: Optional[bool] = Field(None, description="是否支持多语言")
    rule_config: Optional[dict] = Field(None, description="扩展配置")
    severity: Optional[str] = Field(None, max_length=20, description="严重级别")
    is_enabled: Optional[bool] = Field(None, description="是否启用")
    description: Optional[str] = Field(None, description="规则说明")
    example_value: Optional[str] = Field(None, description="示例值")


class MetadataValidationRuleQuery(BaseModel):
    """查询校验规则请求参数。"""
    entity_type: Optional[str] = Field(None, description="实体类型过滤")
    field_name: Optional[str] = Field(None, description="字段名称过滤")
    rule_type: Optional[str] = Field(None, description="规则类型过滤")
    is_mandatory: Optional[str] = Field(None, description="必填标识过滤")
    severity: Optional[str] = Field(None, description="严重级别过滤")
    is_enabled: Optional[bool] = Field(None, description="启用状态过滤")


class MetadataValidationRuleImportResult(BaseModel):
    """导入规则结果。"""
    total: int = Field(0, description="总处理数")
    created: int = Field(0, description="新增数")
    updated: int = Field(0, description="更新数")
    failed: int = Field(0, description="失败数")
    errors: list[str] = Field(default_factory=list, description="错误信息列表")
