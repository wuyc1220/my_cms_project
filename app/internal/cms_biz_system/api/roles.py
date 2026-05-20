from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.models.user import Role
from app.internal.cms_biz_system.schemas.user_crud import BatchIdsRequest, BatchStatusRequest, RoleCreate, RoleListItem, RoleUpdate
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict
from app.internal.cms_biz_system.services.role_service import (
    batch_delete_roles,
    batch_update_role_status,
    create_role,
    delete_role,
    get_all_roles,
    get_role_by_id,
    list_roles,
    toggle_role_status,
    update_role,
)

router = APIRouter(prefix="/roles")


@router.get("/", response_model=PaginatedResponse[RoleListItem])
async def get_roles(
    page: int = 1,
    page_size: int = 10,
    code: str | None = None,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_roles(db, page, page_size, code, name, description, status, sort_by, sort_order)


@router.get("/all", response_model=list[RoleListItem])
async def get_roles_all(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_all_roles(db)


@router.post("/", response_model=RoleListItem)
async def create_role_api(
    body: RoleCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    role = await create_role(db, body)
    new_data = orm_to_dict(role)
    prev_val, new_val, raw_val = await prepare_log_values(db, "role", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.ROLE_CREATE,
        operation_object=f"角色 {role.name}",
        operation_content=f"Created role: name={role.name}, code={role.code}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="role",
        entity_id=role.id,
    )
    await db.commit()
    return RoleListItem.model_validate(role)


@router.get("/{role_id}", response_model=RoleListItem)
async def get_role_detail(
    role_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    role = await get_role_by_id(db, role_id)
    return RoleListItem.model_validate(role)


@router.put("/{role_id}", response_model=RoleListItem)
async def update_role_api(
    role_id: int,
    body: RoleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_role = await get_role_by_id(db, role_id)
    old_data = orm_to_dict(old_role)
    role = await update_role(db, role_id, body)
    new_data = orm_to_dict(role)
    prev_val, new_val, raw_val = await prepare_log_values(db, "role", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.ROLE_EDIT,
        operation_object=f"角色 {role.name}",
        operation_content=f"Updated role: ID={role_id}, name={role.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="role",
        entity_id=role_id,
    )
    await db.commit()
    return RoleListItem.model_validate(role)


@router.delete("/{role_id}")
async def delete_role_api(
    role_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import select
    from app.internal.cms_biz_system.models.user import Role
    role = (await db.execute(select(Role).where(Role.id == role_id))).scalar_one_or_none()
    role_name = role.name if role else str(role_id)
    old_data = orm_to_dict(role) if role else None
    await delete_role(db, role_id)
    prev_val, new_val, raw_val = await prepare_log_values(db, "role", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.ROLE_DELETE,
        operation_object=f"角色 {role_name}",
        operation_content=f"Deleted role: ID={role_id}, name={role_name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="role",
        entity_id=role_id,
    )
    await db.commit()
    return {"success": True}


@router.patch("/{role_id}/status")
async def update_role_status(
    role_id: int,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_role = await get_role_by_id(db, role_id)
    old_data = orm_to_dict(old_role)
    role = await toggle_role_status(db, role_id, body["status"])
    new_data = orm_to_dict(role)
    new_status_label = "enabled" if body["status"] == "active" else "disabled"
    prev_val, new_val, raw_val = await prepare_log_values(db, "role", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.ROLE_STATUS,
        operation_object=f"角色 {role.name}",
        operation_content=f"Changed role status: name={role.name}, new status={new_status_label}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="role",
        entity_id=role_id,
    )
    await db.commit()
    return {"id": role.id, "status": role.status}


@router.post("/batch-status")
async def batch_role_status(
    body: BatchStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    updated = await batch_update_role_status(db, body.ids, body.status)
    rows = (await db.execute(select(Role.name).where(Role.id.in_(body.ids)))).scalars().all()
    role_names = ", ".join(rows) if rows else str(body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.ROLE_BATCH_DELETE if body.status == "deleted" else OperationType.ROLE_BATCH_STATUS,
        operation_object=f"角色 {role_names}",
        operation_content=f"批量{'删除' if body.status == 'deleted' else ('启用' if body.status == 'active' else '禁用')}角色: {role_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="role",
    )
    await db.commit()
    return {"success": True, "updated": updated}


@router.post("/batch-delete")
async def batch_delete_role_api(
    body: BatchIdsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = await batch_delete_roles(db, body.ids)
    rows = (await db.execute(select(Role.name).where(Role.id.in_(body.ids)))).scalars().all()
    role_names = ", ".join(rows) if rows else str(body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.ROLE_BATCH_DELETE,
        operation_object=f"角色 {role_names}",
        operation_content=f"批量删除角色: {role_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="role",
    )
    await db.commit()
    return {"success": True, "deleted": deleted}
