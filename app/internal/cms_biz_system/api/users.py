from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.models.user import User as UserModel
from app.internal.cms_biz_system.schemas.user_crud import BatchIdsRequest, BatchStatusRequest, ResetPasswordRequest, UserCreate, UserListItem, UserUpdate
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict
from app.internal.cms_biz_system.services.user_service import (
    batch_update_user_status,
    build_user_item,
    create_user,
    delete_user,
    get_user,
    list_users,
    reset_password,
    toggle_user_status,
    update_user,
)

router = APIRouter(prefix="/users")


@router.get("/", response_model=PaginatedResponse[UserListItem])
async def get_users(
    page: int = 1,
    page_size: int = 10,
    username: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    phone_number: str | None = None,
    status: str | None = None,
    role_ids: list[int] | None = Query(None),
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_users(db, page, page_size, username, display_name, email, phone_number, status, role_ids, sort_by, sort_order)


@router.post("/", response_model=UserListItem)
async def create_user_api(
    body: UserCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    user = await create_user(db, body)
    result = build_user_item(user)
    new_data = orm_to_dict(user, "user")
    prev_val, new_val, raw_val = await prepare_log_values(db, "user", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_CREATE,
        operation_object=f"用户 {body.username}",
        operation_content=f"Created user: username={body.username}, display name={body.display_name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="user",
        entity_id=user.id,
    )
    await db.commit()
    return result


@router.get("/{user_id}", response_model=UserListItem)
async def get_user_detail(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    user = await get_user(db, user_id)
    return build_user_item(user)


@router.put("/{user_id}", response_model=UserListItem)
async def update_user_api(
    user_id: int,
    body: UserUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = await get_user(db, user_id)
    old_data = orm_to_dict(target, "user")
    old_username = target.username
    user = await update_user(db, user_id, body)
    result = build_user_item(user)
    new_data = orm_to_dict(user, "user")
    prev_val, new_val, raw_val = await prepare_log_values(db, "user", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_EDIT,
        operation_object=f"用户 {old_username}",
        operation_content=f"Updated user: ID={user_id}, username={old_username}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="user",
        entity_id=user_id,
    )
    await db.commit()
    return result


@router.post("/{user_id}/reset-password")
async def reset_user_password(
    user_id: int,
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = await get_user(db, user_id)
    await reset_password(db, user_id, body.new_password, body.confirm_password)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_RESET_PWD,
        operation_object=f"用户 {target.username}",
        operation_content=f"Reset password for user: username={target.username}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="user",
        entity_id=user_id,
    )
    await db.commit()
    return {"success": True}


@router.patch("/{user_id}/status")
async def update_user_status(
    user_id: int,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = await get_user(db, user_id)
    old_data = orm_to_dict(target, "user")
    user = await toggle_user_status(db, user_id, body["status"])
    new_data = orm_to_dict(user, "user")
    new_status_label = "enabled" if body["status"] == "active" else "disabled"
    prev_val, new_val, raw_val = await prepare_log_values(db, "user", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_STATUS,
        operation_object=f"用户 {target.username}",
        operation_content=f"Changed user status: username={target.username}, new status={new_status_label}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="user",
        entity_id=user_id,
    )
    await db.commit()
    return {"id": user.id, "status": user.status}


@router.post("/batch-status")
async def batch_user_status(
    body: BatchStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    updated = await batch_update_user_status(db, body.ids, body.status)
    rows = (await db.execute(select(UserModel.username).where(UserModel.id.in_(body.ids)))).scalars().all()
    user_names = ", ".join(rows) if rows else str(body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_BATCH_DELETE if body.status == "deleted" else OperationType.USER_BATCH_STATUS,
        operation_object=f"用户 {user_names}",
        operation_content=f"批量{'删除' if body.status == 'deleted' else ('启用' if body.status == 'active' else '禁用')}用户: {user_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="user",
    )
    await db.commit()
    return {"success": True, "updated": updated}


@router.delete("/{user_id}")
async def delete_user_api(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    target = await get_user(db, user_id)
    old_data = orm_to_dict(target, "user")
    await delete_user(db, user_id)
    prev_val, new_val, raw_val = await prepare_log_values(db, "user", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_DELETE,
        operation_object=f"用户 {target.username}",
        operation_content=f"Deleted user: username={target.username}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="user",
        entity_id=user_id,
    )
    await db.commit()
    return {"success": True}


@router.post("/batch-delete")
async def batch_delete_users_api(
    body: BatchIdsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.internal.cms_biz_system.services.user_service import batch_delete_users
    rows = (await db.execute(select(UserModel.username).where(UserModel.id.in_(body.ids)))).scalars().all()
    user_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await batch_delete_users(db, body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_BATCH_DELETE,
        operation_object=f"用户 {user_names}",
        operation_content=f"批量删除用户: {user_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="user",
    )
    await db.commit()
    return {"success": True, "deleted": deleted}
