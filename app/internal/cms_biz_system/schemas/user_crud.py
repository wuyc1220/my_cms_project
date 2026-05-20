from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


# ---------- Role ----------

class RoleListItem(BaseModel):
    id: int
    name: str
    code: str
    status: str
    description: str | None = None
    is_system: bool = False
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class RoleCreate(BaseModel):
    name: str = Field(..., max_length=100)
    code: str | None = Field(None, max_length=100)
    status: str = "active"
    description: str | None = Field(None, max_length=500)


class RoleUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    status: str | None = None
    description: str | None = Field(None, max_length=500)


# ---------- User ----------

class UserListItem(BaseModel):
    id: int
    username: str
    display_name: str | None = None
    email: str | None = None
    phone_number: str | None = None
    status: str
    roles: list[RoleListItem] = []
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserCreate(BaseModel):
    username: str = Field(..., max_length=100)
    password: str = Field(..., max_length=100)
    display_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=100)
    phone_number: str | None = Field(None, max_length=30)
    status: str = "active"
    role_ids: list[int] = []


class UserUpdate(BaseModel):
    display_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=100)
    phone_number: str | None = Field(None, max_length=30)
    status: str | None = None
    role_ids: list[int] | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str = Field(..., min_length=1, max_length=100)
    confirm_password: str = Field(..., min_length=1, max_length=100)


# ---------- Config ----------

class ConfigListItem(BaseModel):
    id: int
    config_key: str
    config_name: str
    config_value: str | None = None
    description: str | None = None
    is_system: bool = False
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ConfigCreate(BaseModel):
    config_key: str = Field(..., max_length=100)
    config_name: str = Field(..., max_length=100)
    config_value: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=500)


class ConfigUpdate(BaseModel):
    config_name: str | None = Field(None, max_length=100)
    config_value: str | None = Field(None, max_length=500)
    description: str | None = Field(None, max_length=500)


# ---------- Dict ----------

class DictNodeListItem(BaseModel):
    id: int
    parent_id: int | None = None
    code: str
    name: str
    sort_order: int = 0
    status: str
    remark: str | None = None
    is_system: bool = False
    children: list["DictNodeListItem"] = []

    model_config = ConfigDict(from_attributes=True)


class DictNodeCreate(BaseModel):
    parent_id: int | None = None
    code: str | None = Field(None, max_length=100)
    name: str = Field(..., max_length=100)
    sort_order: int = 0
    remark: str | None = Field(None, max_length=500)


class DictNodeUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    sort_order: int | None = None
    remark: str | None = Field(None, max_length=500)


class BatchStatusRequest(BaseModel):
    ids: list[int]
    status: str


class BatchIdsRequest(BaseModel):
    ids: list[int]


DictNodeListItem.model_rebuild()


# ---------- i18n ----------

class LanguageConfigResponse(BaseModel):
    language: str


class LanguageOption(BaseModel):
    code: str
    name: str
