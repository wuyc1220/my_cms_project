"""
注入历史（Ingest History）Pydantic 数据模型。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class IngestHistoryItem(BaseModel):
    """注入历史记录项。"""

    id: int
    entity_type: str
    entity_id: int
    entity_name: Optional[str] = None
    action: str
    status: str
    create_date: Optional[datetime] = None
    send_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    ingest_xml_path: Optional[str] = None
    result_xml_path: Optional[str] = None
    ingest_xml_url: Optional[str] = None
    result_xml_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class IngestHistoryDetailItem(BaseModel):
    """注入历史明细记录项（关联对象视角）。"""

    id: int
    history_id: int
    entity_type: str
    entity_id: int
    entity_name: Optional[str] = None
    action: str
    created_at: Optional[datetime] = None
    trigger_content_id: Optional[int] = None
    trigger_content_name: Optional[str] = None
    status: Optional[str] = None
    create_date: Optional[datetime] = None
    send_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    ingest_xml_path: Optional[str] = None
    result_xml_path: Optional[str] = None
    ingest_xml_url: Optional[str] = None
    result_xml_url: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class IngestHistoryQuery(BaseModel):
    """注入历史查询参数。"""

    entity_type: Optional[str] = None
    entity_id: Optional[int] = None
    action: Optional[str] = None
    status: Optional[str] = None
    page: int = 1
    page_size: int = 10
