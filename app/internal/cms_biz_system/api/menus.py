"""菜单管理 API"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_db, get_current_user, _get_ip
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.menu import (
    MenuItemFlat,
    MenuItemTree,
    MenuCreate,
    MenuUpdate,
    RoleMenuAssign,
)
from app.internal.cms_biz_system.services.menu_service import (
    get_menu_tree,
    get_user_menu_tree,
    create_menu,
    update_menu_by_id,
    delete_menu_by_id,
    assign_role_menus,
    get_role_menu_ids,
)

router = APIRouter(prefix="/menus")


@router.get("/user", response_model=list[MenuItemTree])
async def get_user_menus(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取当前用户的菜单树（根据角色权限过滤）"""
    # 获取用户的 role_codes
    from sqlalchemy import select
    from app.internal.cms_biz_system.models.user import Role, UserRole

    codes_result = await db.execute(
        select(Role.code)
        .join(UserRole, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == current_user.id,
            Role.is_deleted.is_(False),
            Role.status == "active",
        )
    )
    role_codes = list(codes_result.scalars().all())

    return await get_user_menu_tree(db, current_user.id, role_codes)


@router.get("/", response_model=list[MenuItemTree])
async def get_all_menus(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """获取完整菜单树（管理用）"""
    return await get_menu_tree(db)


@router.post("/", response_model=MenuItemFlat)
async def create_menu_api(
    body: MenuCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建菜单"""
    menu = await create_menu(db, body)
    new_data = orm_to_dict(menu)
    prev_val, new_val, raw_val = await prepare_log_values(db, "menu", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MENU_CREATE,
        operation_object=f"菜单 {body.name}",
        operation_content=f"Created menu: name={body.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="menu",
        entity_id=menu.id,
    )
    await db.commit()
    return menu


@router.put("/{menu_id}", response_model=MenuItemFlat)
async def update_menu_api(
    menu_id: int,
    body: MenuUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新菜单"""
    menu = await update_menu_by_id(db, menu_id, body)
    new_data = orm_to_dict(menu)
    prev_val, new_val, raw_val = await prepare_log_values(db, "menu", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MENU_EDIT,
        operation_object=f"菜单 #{menu_id}",
        operation_content=f"Updated menu: ID={menu_id}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="menu",
        entity_id=menu_id,
    )
    await db.commit()
    return menu


@router.delete("/{menu_id}")
async def delete_menu_api(
    menu_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除菜单"""
    await delete_menu_by_id(db, menu_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MENU_DELETE,
        operation_object=f"菜单 #{menu_id}",
        operation_content=f"Deleted menu: ID={menu_id}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="menu",
        entity_id=menu_id,
    )
    await db.commit()
    return {"success": True}


@router.get("/roles/{role_id}/ids", response_model=list[int])
async def get_role_menu_ids_api(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """获取角色关联的菜单ID列表"""
    return await get_role_menu_ids(db, role_id)


@router.put("/roles/{role_id}", response_model=dict)
async def assign_role_menus_api(
    role_id: int,
    body: RoleMenuAssign,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """分配角色菜单权限"""
    await assign_role_menus(db, role_id, body.menu_ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MENU_ASSIGN,
        operation_object=f"角色 #{role_id} 菜单权限",
        operation_content=f"Assigned menus to role: role_id={role_id}, menu_ids={body.menu_ids}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="menu",
    )
    await db.commit()
    return {"success": True}
