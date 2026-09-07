from datetime import datetime

from pydantic import BaseModel, ConfigDict


class OperationLogItem(BaseModel):
    id: int
    user_id: int | None = None
    user_name: str | None = None
    user_display_name: str | None = None
    operation_type: str | None = None
    operation_object: str | None = None
    operation_content: str | None = None
    content_id: int | None = None
    entity_type: str | None = None
    entity_id: int | None = None
    previous_value: str | None = None
    updated_value: str | None = None
    updated_value_json: str | None = None
    operation_time: datetime
    ip_address: str | None = None
    result: str
    error_message: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ProcessedHistoryItem(BaseModel):
    id: int
    processed_at: datetime | None = None
    processed_by: str | None = None
    processed_by_display_name: str | None = None
    processed_type: str | None = None
    entity_type: str | None = None
    details: str | None = None
    previous_value: str | None = None
    updated_value: str | None = None
    updated_value_json: str | None = None

    model_config = ConfigDict(from_attributes=True)


class ClearLogsRequest(BaseModel):
    start: datetime
    end: datetime
