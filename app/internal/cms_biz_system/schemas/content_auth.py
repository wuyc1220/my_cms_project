"""数据权限管理 - Pydantic 数据模型"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


# ─── 角色/用户简要信息（下拉选项）────────────────────────────────

class RoleSimpleItem(BaseModel):
    id: int
    name: str
    code: str | None = None
    model_config = ConfigDict(from_attributes=True)


class UserSimpleItem(BaseModel):
    id: int
    display_name: str | None = None
    username: str
    model_config = ConfigDict(from_attributes=True)

    @property
    def label(self) -> str:
        name = self.display_name or self.username
        return f"{name}（{self.username}）"


# ─── 内容列表项（含授权信息）────────────────────────────────────

class AuthorizedRoleItem(BaseModel):
    id: int
    name: str
    model_config = ConfigDict(from_attributes=True)


class AuthorizedUserItem(BaseModel):
    id: int
    display_name: str | None = None
    username: str
    model_config = ConfigDict(from_attributes=True)


class ContentAuthListItem(BaseModel):
    """内容列表项（带授权角色和用户）"""
    id: int
    content_name: str
    content_type: str
    ingest_status: str
    authorized_roles: list[AuthorizedRoleItem] = []
    authorized_users: list[AuthorizedUserItem] = []


# ─── 查询参数 ────────────────────────────────────────────────────

class ContentAuthQueryParams(BaseModel):
    page: int = 1
    page_size: int = 10
    content_name: Optional[str] = None
    content_types: Optional[list[str]] = None
    ingest_statuses: Optional[list[str]] = None
    authorized_user: Optional[str] = None
    authorized_role: Optional[str] = None


# ─── 授权请求体 ──────────────────────────────────────────────────

class ContentAuthAuthorizePayload(BaseModel):
    content_ids: list[int]
    role_ids: list[int] | None = None
    user_ids: list[int] | None = None


# ─── 清除请求体 ──────────────────────────────────────────────────

class ContentAuthClearPayload(BaseModel):
    content_ids: list[int]
