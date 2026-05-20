"""菜单管理模型

提供 cms_menu（菜单）和 role_menu（角色-菜单关联）两张表。
菜单采用 parent_id 自引用实现树形结构。
"""

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow():
    return datetime.now(timezone.utc)


class Menu(Base):
    """菜单表"""
    __tablename__ = "cms_menu"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cms_menu.id"), nullable=True, comment="父菜单ID，NULL表示顶级"
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="菜单名称（后台标识）")
    i18n_key: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="国际化key，如 menu.trade.providers"
    )
    path: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="前端路由路径，NULL表示分组"
    )
    icon: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="图标名，如 ShoppingOutlined"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, default=0, comment="同级排序，越小越靠前"
    )
    status: Mapped[str] = mapped_column(
        String(20), default="active", comment="active / disabled"
    )
    is_external: Mapped[bool] = mapped_column(
        Boolean, default=False, comment="是否外链（预留）"
    )
    menu_type: Mapped[str] = mapped_column(
        String(20), default="menu", comment="menu=导航菜单, permission=权限点"
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False, comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )

    # 自引用关系
    children: Mapped[list["Menu"]] = relationship(
        "Menu", backref="parent", remote_side=[id], lazy="selectin"
    )
    # 角色-菜单关联
    role_menus: Mapped[list["RoleMenu"]] = relationship(
        "RoleMenu", back_populates="menu", cascade="all, delete-orphan"
    )


class RoleMenu(Base):
    """角色-菜单关联表"""
    __tablename__ = "role_menu"

    id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True
    )
    role_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("role.id", ondelete="CASCADE"), nullable=False
    )
    menu_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cms_menu.id", ondelete="CASCADE"), nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )

    role: Mapped["Role"] = relationship("Role", back_populates="role_menus")
    menu: Mapped["Menu"] = relationship("Menu", back_populates="role_menus")
