from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class SensitiveWordListItem(BaseModel):
    id: int
    keyword: str
    type_code: str
    status: str
    is_deleted: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class SensitiveWordCreate(BaseModel):
    keyword: str = Field(..., max_length=100)
    type_code: str = Field(..., max_length=100)
    status: str = "active"


class SensitiveWordUpdate(BaseModel):
    keyword: str | None = Field(None, max_length=100)
    type_code: str | None = Field(None, max_length=100)
    status: str | None = None


class StatusRequest(BaseModel):
    status: str


class BatchStatusRequest(BaseModel):
    ids: list[int]
    status: str


class ImportResult(BaseModel):
    total: int = 0
    created: int = 0
    updated: int = 0
