"""
菜单管理 - 数据访问层
Menu / RoleMenu 的 SQLAlchemy 操作
"""

from sqlalchemy import delete, select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_system.models.menu import Menu, RoleMenu


# ═══════════════════════════════════════════════════════════
# Menu CRUD
# ═══════════════════════════════════════════════════════════

async def get_menu_by_id(db: AsyncSession, menu_id: int) -> Menu | None:
    return (await db.execute(
        select(Menu).where(Menu.id == menu_id, Menu.is_deleted.is_(False))
    )).scalar_one_or_none()


async def get_all_menus(db: AsyncSession) -> list[Menu]:
    """获取所有菜单（按 sort_order 排序）"""
    result = await db.execute(
        select(Menu).where(Menu.is_deleted.is_(False))
        .order_by(Menu.sort_order.asc(), Menu.id.asc())
    )
    return list(result.scalars().all())


async def get_active_menus(db: AsyncSession) -> list[Menu]:
    """获取所有启用的菜单"""
    result = await db.execute(
        select(Menu)
        .where(Menu.status == "active", Menu.is_deleted.is_(False))
        .order_by(Menu.sort_order.asc(), Menu.id.asc())
    )
    return list(result.scalars().all())


async def get_menus_by_ids(db: AsyncSession, menu_ids: list[int]) -> list[Menu]:
    """根据 ID 列表获取菜单"""
    if not menu_ids:
        return []
    result = await db.execute(
        select(Menu).where(Menu.id.in_(menu_ids), Menu.status == "active", Menu.is_deleted.is_(False))
        .order_by(Menu.sort_order.asc(), Menu.id.asc())
    )
    return list(result.scalars().all())


async def add_menu(db: AsyncSession, data: dict) -> Menu:
    menu = Menu(**data)
    db.add(menu)
    await db.flush()
    return menu


async def update_menu(db: AsyncSession, menu: Menu, data: dict) -> Menu:
    for key, value in data.items():
        if value is not None:
            setattr(menu, key, value)
    await db.flush()
    return menu


async def delete_menu(db: AsyncSession, menu_id: int) -> bool:
    """删除菜单（软删除）"""
    menu = await get_menu_by_id(db, menu_id)
    if not menu:
        return False
    # 软删除角色关联
    await db.execute(
        update(RoleMenu).where(RoleMenu.menu_id == menu_id, RoleMenu.is_deleted == False).values(is_deleted=True)
    )
    # 软删除子菜单
    child_ids = (
        await db.execute(select(Menu.id).where(Menu.parent_id == menu_id, Menu.is_deleted.is_(False)))
    ).scalars().all()
    if child_ids:
        await db.execute(
            update(RoleMenu).where(RoleMenu.menu_id.in_(child_ids), RoleMenu.is_deleted == False).values(is_deleted=True)
        )
        await db.execute(
            update(Menu).where(Menu.id.in_(child_ids)).values(is_deleted=True)
        )
    # 软删除菜单本身
    menu.is_deleted = True
    await db.flush()
    return True


async def get_menu_children(db: AsyncSession, parent_id: int) -> list[Menu]:
    """获取某个菜单的子菜单"""
    result = await db.execute(
        select(Menu)
        .where(Menu.parent_id == parent_id, Menu.status == "active", Menu.is_deleted.is_(False))
        .order_by(Menu.sort_order.asc(), Menu.id.asc())
    )
    return list(result.scalars().all())


# ═══════════════════════════════════════════════════════════
# RoleMenu
# ═══════════════════════════════════════════════════════════

async def get_menu_ids_by_role_id(db: AsyncSession, role_id: int) -> list[int]:
    """获取角色关联的菜单ID列表（排除已软删除）"""
    result = await db.execute(
        select(RoleMenu.menu_id).where(RoleMenu.role_id == role_id, RoleMenu.is_deleted == False)
    )
    return list(result.scalars().all())


async def get_menu_ids_by_role_ids(db: AsyncSession, role_ids: list[int]) -> list[int]:
    """获取多个角色关联的菜单ID列表（去重，排除已软删除）"""
    if not role_ids:
        return []
    result = await db.execute(
        select(RoleMenu.menu_id)
        .where(RoleMenu.role_id.in_(role_ids), RoleMenu.is_deleted == False)
        .distinct()
    )
    return list(result.scalars().all())


async def get_menu_ids_by_user_roles(db: AsyncSession, user_id: int) -> list[int]:
    """通过用户ID获取其角色关联的菜单ID列表（仅有效角色）"""
    from app.internal.cms_biz_system.models.user import UserRole, Role
    role_ids_result = await db.execute(
        select(UserRole.role_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == user_id,
            Role.is_deleted.is_(False),
            Role.status == "active",
        )
    )
    role_ids = list(role_ids_result.scalars().all())
    if not role_ids:
        return []
    return await get_menu_ids_by_role_ids(db, role_ids)


async def replace_role_menus(db: AsyncSession, role_id: int, menu_ids: list[int]) -> None:
    """替换角色的菜单关联（软删除旧的，新增/恢复新的）"""
    # 先软删除当前所有关联
    await db.execute(
        update(RoleMenu)
        .where(RoleMenu.role_id == role_id, RoleMenu.is_deleted == False)
        .values(is_deleted=True)
    )
    # 对新列表中的每个 menu_id，查找是否已有记录
    for menu_id in menu_ids:
        existing = (
            await db.execute(
                select(RoleMenu).where(
                    RoleMenu.role_id == role_id,
                    RoleMenu.menu_id == menu_id
                )
            )
        ).scalar_one_or_none()
        if existing:
            # 已有记录，恢复
            existing.is_deleted = False
        else:
            # 新增记录
            db.add(RoleMenu(role_id=role_id, menu_id=menu_id, is_deleted=False))
    await db.flush()


async def has_role_menu_ref(db: AsyncSession, menu_id: int) -> bool:
    """检查菜单是否被角色引用（排除已软删除）"""
    result = await db.execute(
        select(func.count()).select_from(RoleMenu).where(
            RoleMenu.menu_id == menu_id, RoleMenu.is_deleted == False
        )
    )
    return result.scalar_one() > 0
