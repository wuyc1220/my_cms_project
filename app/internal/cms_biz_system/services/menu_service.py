"""
菜单管理 - 业务逻辑层
"""

from app.common.core.i18n import get_msg
from app.common.core.exceptions import BusinessException, NotFoundException, ErrorCode
from app.internal.cms_biz_system.models.menu import Menu
from app.internal.cms_biz_system.models.user import Role, UserRole
from app.internal.cms_biz_system.repositories.menu_repository import (
    get_menu_by_id,
    get_all_menus,
    get_active_menus,
    get_menus_by_ids,
    add_menu as repo_add_menu,
    update_menu as repo_update_menu,
    delete_menu as repo_delete_menu,
    get_menu_ids_by_role_id,
    get_menu_ids_by_user_roles,
    replace_role_menus,
)
from app.internal.cms_biz_system.schemas.menu import (
    MenuItemFlat,
    MenuItemTree,
    MenuCreate,
    MenuUpdate,
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def _build_tree(flat_items: list[MenuItemFlat]) -> list[MenuItemTree]:
    """将扁平菜单列表构建为树形结构"""
    item_map: dict[int, MenuItemTree] = {}
    roots: list[MenuItemTree] = []

    for item in flat_items:
        tree_item = MenuItemTree(
            id=item.id,
            parent_id=item.parent_id,
            name=item.name,
            i18n_key=item.i18n_key,
            path=item.path,
            icon=item.icon,
            sort_order=item.sort_order,
            status=item.status,
            is_external=item.is_external,
            menu_type=item.menu_type,
            children=[],
        )
        item_map[item.id] = tree_item

    for item in flat_items:
        if item.parent_id is None:
            roots.append(item_map[item.id])
        else:
            parent = item_map.get(item.parent_id)
            if parent:
                parent.children.append(item_map[item.id])

    return roots


async def get_menu_tree(db: AsyncSession) -> list[MenuItemTree]:
    """获取完整菜单树（管理用，包含 disabled 的菜单）"""
    menus = await get_all_menus(db)
    flat_items = [MenuItemFlat.model_validate(m) for m in menus]
    return _build_tree(flat_items)


async def get_user_menu_tree(db: AsyncSession, user_id: int, role_codes: list[str]) -> list[MenuItemTree]:
    """根据用户角色获取可见菜单树

    规则：
    - admin 角色拥有所有菜单和权限点
    - 其他菜单根据 role_menu 关联过滤
    - 如果父菜单下无可见子菜单，则自动隐藏父菜单
    - 返回导航菜单（menu_type=menu）和权限点（menu_type=permission），
      权限点作为子节点挂在对应页面菜单下，供前端提取权限信息
    """
    all_menus = await get_active_menus(db)
    all_flat = [MenuItemFlat.model_validate(m) for m in all_menus]

    # admin 拥有所有菜单和权限点
    is_admin = "ADMIN" in [c.upper() for c in role_codes]
    if is_admin:
        return _build_tree(all_flat)

    # 非 admin 角色过滤：排除"菜单管理"（仅 admin 可用）
    non_admin_flat = [m for m in all_flat if m.i18n_key != "menu.system.menus"]

    # 获取用户角色关联的菜单ID（包含权限点）
    visible_ids = set(await get_menu_ids_by_user_roles(db, user_id))

    # 如果用户只勾选了权限点（menu_type=permission），需要把对应的父菜单也加入可见列表
    # 例如：勾选了"供应商查看权限"(id=38, parent_id=9)，则"供应商管理"(id=9)也应可见
    perm_items = [m for m in non_admin_flat if m.id in visible_ids and m.menu_type == "permission"]
    for perm in perm_items:
        if perm.parent_id is not None:
            visible_ids.add(perm.parent_id)

    # 过滤可见的菜单和权限点
    visible_flat = [m for m in non_admin_flat if m.id in visible_ids]

    # 补充父菜单（即使父菜单不在 visible_ids 中，只要有子菜单可见就展示）
    child_parent_ids = {m.parent_id for m in visible_flat if m.parent_id is not None}
    for m in non_admin_flat:
        if m.id in child_parent_ids and m not in visible_flat:
            visible_flat.append(m)

    # 重新排序
    visible_flat.sort(key=lambda x: (x.sort_order, x.id))

    tree = _build_tree(visible_flat)

    # 移除没有可见子菜单的分组菜单（保留权限点节点）
    def _filter_empty_groups(items: list[MenuItemTree]) -> list[MenuItemTree]:
        result = []
        for item in items:
            if item.children:
                item.children = _filter_empty_groups(item.children)
            # 有 path 的是页面菜单，保留；权限点保留；分组需要有 children 才保留
            if item.path or item.menu_type == 'permission' or item.children:
                result.append(item)
        return result

    return _filter_empty_groups(tree)


async def create_menu(db: AsyncSession, data: MenuCreate) -> MenuItemFlat:
    """创建菜单"""
    # 校验 parent_id 有效性
    if data.parent_id is not None:
        parent = await get_menu_by_id(db, data.parent_id)
        if not parent:
            raise BusinessException(ErrorCode.PARENT_MENU_NOT_FOUND, get_msg("PARENT_MENU_NOT_FOUND"))

    menu = await repo_add_menu(db, data.model_dump())
    await db.commit()
    await db.refresh(menu)
    return MenuItemFlat.model_validate(menu)


async def update_menu_by_id(db: AsyncSession, menu_id: int, data: MenuUpdate) -> MenuItemFlat:
    """更新菜单"""
    menu = await get_menu_by_id(db, menu_id)
    if not menu:
        raise BusinessException(ErrorCode.MENU_NOT_FOUND, get_msg("MENU_NOT_FOUND"))

    # 校验 parent_id 有效性（防止循环引用）
    update_data = data.model_dump(exclude_none=True)
    if "parent_id" in update_data:
        if update_data["parent_id"] is not None:
            if update_data["parent_id"] == menu_id:
                raise BusinessException(ErrorCode.MENU_CIRCULAR_REF, get_msg("MENU_CIRCULAR_REF"))
            parent = await get_menu_by_id(db, update_data["parent_id"])
            if not parent:
                raise BusinessException(ErrorCode.PARENT_MENU_NOT_FOUND, get_msg("PARENT_MENU_NOT_FOUND"))

    menu = await repo_update_menu(db, menu, update_data)
    await db.commit()
    await db.refresh(menu)
    return MenuItemFlat.model_validate(menu)


async def delete_menu_by_id(db: AsyncSession, menu_id: int) -> bool:
    """删除菜单"""
    result = await repo_delete_menu(db, menu_id)
    if not result:
        raise NotFoundException(ErrorCode.NOT_FOUND, get_msg("MENU_NOT_FOUND"))
    await db.commit()
    return True


async def assign_role_menus(db: AsyncSession, role_id: int, menu_ids: list[int]) -> None:
    """分配角色菜单权限"""
    # 校验角色存在
    from app.internal.cms_biz_system.repositories import get_role_by_id
    role = await get_role_by_id(db, role_id)
    if not role:
        raise BusinessException(ErrorCode.ROLE_NOT_FOUND, get_msg("ROLE_NOT_FOUND"))

    # 校验菜单ID有效
    if menu_ids:
        valid_menus = await get_menus_by_ids(db, menu_ids)
        valid_ids = {m.id for m in valid_menus}
        invalid_ids = set(menu_ids) - valid_ids
        if invalid_ids:
            raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("INVALID_MENU_IDS", ids=invalid_ids))

    await replace_role_menus(db, role_id, menu_ids)
    await db.commit()


async def get_role_menu_ids(db: AsyncSession, role_id: int) -> list[int]:
    """获取角色的菜单ID列表"""
    return await get_menu_ids_by_role_id(db, role_id)
