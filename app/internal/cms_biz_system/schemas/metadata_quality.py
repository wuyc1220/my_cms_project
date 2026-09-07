"""元数据质量检查（MetadataQuality）相关 Pydantic schemas。"""

import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.common.core.i18n import get_msg


def _translate_issue_value(value: str | None) -> str | None:
    """
    翻译质检引擎写入的 i18n 描述值（按当前请求语言）。

    存储格式（语言无关，见 metadata_quality_check_service._i18n_value/_i18n_list）：
      {"key": "...", "params": {...}}  单条消息，params 的字符串值递归翻译
      {"items": [...], "sep": "..."}   列表拼接，逐项递归翻译后以 sep 连接
    非 JSON 的值（如历史数据、纯数据值）原样返回。
    """
    if not value:
        return value
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return value
    if isinstance(parsed, dict):
        if isinstance(parsed.get("key"), str):
            params = {
                k: _translate_issue_value(v) if isinstance(v, str) else v
                for k, v in (parsed.get("params") or {}).items()
            }
            return get_msg(parsed["key"], **params)
        if isinstance(parsed.get("items"), list):
            # sep 支持传入 i18n 值（如 _i18n_value("MQ_SEP_ENUM")），纯字符串原样使用
            sep = _translate_issue_value(parsed.get("sep", "、")) or "、"
            return sep.join(
                _translate_issue_value(i) or "" if isinstance(i, str) else str(i)
                for i in parsed["items"]
            )
    return value


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
    issue_type: Literal["missing", "format", "invalid", "sensitive", "authorization"]
    field_name: str | None = ""
    severity: Literal["critical", "medium", "minor"] | None = None
    expected_value: str | None = ""
    actual_value: str | None = ""

    @field_validator("expected_value", "actual_value", mode="after")
    @classmethod
    def _i18n(cls, v: str | None) -> str | None:
        return _translate_issue_value(v)


class MetadataQualityCheckDetail(MetadataQualityCheckOut):
    issues: list[MetadataQualityIssueOut] = []


class TriggerMetadataCheckResponse(BaseModel):
    id: int


class DeleteMetadataChecksResponse(BaseModel):
    deleted: int
