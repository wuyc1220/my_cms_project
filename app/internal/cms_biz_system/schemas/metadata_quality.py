"""元数据质量检查（MetadataQuality）相关 Pydantic schemas。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class MetadataQualityCheckOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    check_time: datetime
    status: Literal["pending", "running", "completed", "failed"]
    total_contents: int
    passed_count: int
    failed_count: int
    duration: float | None = None


class MetadataQualityIssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    content_id: int
    content_name: str | None = ""
    content_type: str | None = ""
    issue_type: Literal["missing", "format", "invalid"]
    field_name: str | None = ""
    severity: Literal["critical", "medium", "minor"] | None = None
    expected_value: str | None = ""
    actual_value: str | None = ""


class MetadataQualityCheckDetail(MetadataQualityCheckOut):
    issues: list[MetadataQualityIssueOut] = []


class TriggerMetadataCheckResponse(BaseModel):
    id: int


class DeleteMetadataChecksResponse(BaseModel):
    deleted: int
