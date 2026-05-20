"""菜单管理 Schema"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class MenuItemFlat(BaseModel):
    """扁平菜单项（后端返回给前端的原始结构）"""
    id: int
    parent_id: int | None = None
    name: str
    i18n_key: str
    path: str | None = None
    icon: str | None = None
    sort_order: int = 0
    status: str = "active"
    is_external: bool = False
    menu_type: str = "menu"

    model_config = ConfigDict(from_attributes=True)


class MenuItemTree(MenuItemFlat):
    """树形菜单项（含子菜单）"""
    children: list["MenuItemTree"] = []

    model_config = ConfigDict(from_attributes=True)


class MenuCreate(BaseModel):
    """创建菜单"""
    parent_id: int | None = None
    name: str = Field(..., max_length=100)
    i18n_key: str = Field(..., max_length=100)
    path: str | None = Field(None, max_length=100)
    icon: str | None = Field(None, max_length=100)
    sort_order: int = 0
    status: str = "active"
    is_external: bool = False
    menu_type: str = "menu"


class MenuUpdate(BaseModel):
    """更新菜单"""
    parent_id: int | None = None
    name: str | None = Field(None, max_length=100)
    i18n_key: str | None = Field(None, max_length=100)
    path: str | None = Field(None, max_length=100)
    icon: str | None = Field(None, max_length=100)
    sort_order: int | None = None
    status: str | None = None
    is_external: bool | None = None
    menu_type: str | None = None


class RoleMenuAssign(BaseModel):
    """角色分配菜单权限"""
    menu_ids: list[int]


class RoleMenuItem(BaseModel):
    """角色详情中返回的菜单ID列表"""
    menu_ids: list[int] = []
