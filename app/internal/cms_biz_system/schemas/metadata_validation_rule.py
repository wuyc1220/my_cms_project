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
    """Create validation rule request."""
    entity_type: str = Field(..., max_length=50, description="Entity type")
    field_name: str = Field(..., max_length=100, description="Field name")
    rule_type: str = Field(..., max_length=50, description="Rule type")
    is_mandatory: str = Field("N", max_length=1, description="Required flag: Y/N/C")
    mandatory_condition: Optional[str] = Field(None, description="Conditional required trigger (JSON)")
    max_length: Optional[int] = Field(None, ge=0, description="Max length")
    min_length: Optional[int] = Field(None, ge=0, description="Min length")
    allowed_values: Optional[str] = Field(None, description="Allowed values")
    regex_pattern: Optional[str] = Field(None, max_length=500, description="Regex pattern")
    data_type: Optional[str] = Field(None, max_length=50, description="Data type")
    multi_language: bool = Field(False, description="Multi-language support")
    rule_config: Optional[dict] = Field(None, description="Extended config (JSONB)")
    severity: str = Field("medium", max_length=20, description="Severity level")
    is_enabled: bool = Field(True, description="Is enabled")
    is_system: bool = Field(True, description="Is system built-in")
    description: Optional[str] = Field(None, description="Rule description")
    example_value: Optional[str] = Field(None, description="Example value")


class MetadataValidationRuleUpdate(BaseModel):
    """Update validation rule request."""
    is_mandatory: Optional[str] = Field(None, max_length=1, description="Required flag")
    mandatory_condition: Optional[str] = Field(None, description="Conditional required trigger")
    max_length: Optional[int] = Field(None, ge=0, description="Max length")
    min_length: Optional[int] = Field(None, ge=0, description="Min length")
    allowed_values: Optional[str] = Field(None, description="Allowed values")
    regex_pattern: Optional[str] = Field(None, max_length=500, description="Regex pattern")
    data_type: Optional[str] = Field(None, max_length=50, description="Data type")
    multi_language: Optional[bool] = Field(None, description="Multi-language support")
    rule_config: Optional[dict] = Field(None, description="Extended config")
    severity: Optional[str] = Field(None, max_length=20, description="Severity level")
    is_enabled: Optional[bool] = Field(None, description="Is enabled")
    description: Optional[str] = Field(None, description="Rule description")
    example_value: Optional[str] = Field(None, description="Example value")


class MetadataValidationRuleQuery(BaseModel):
    """Query validation rule params."""
    entity_type: Optional[str] = Field(None, description="Entity type filter")
    field_name: Optional[str] = Field(None, description="Field name filter")
    rule_type: Optional[str] = Field(None, description="Rule type filter")
    is_mandatory: Optional[str] = Field(None, description="Required flag filter")
    severity: Optional[str] = Field(None, description="Severity level filter")
    is_enabled: Optional[bool] = Field(None, description="Enabled status filter")


class MetadataValidationRuleImportResult(BaseModel):
    """Import rule result."""
    total: int = Field(0, description="Total processed")
    created: int = Field(0, description="Created")
    updated: int = Field(0, description="Updated")
    failed: int = Field(0, description="Failed")
    errors: list[str] = Field(default_factory=list, description="Error list")
