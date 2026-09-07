import json

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
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict, _json_default
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
    if body.role_ids is not None:
        new_data["role_ids"] = body.role_ids
    else:
        new_data["role_ids"] = []
    prev_val, new_val, raw_val = await prepare_log_values(db, "user", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_CREATE,
        operation_object_code="OBJ_USER", operation_object_params={"name": body.username},
        operation_content_code="LOG_USER_CREATE", operation_content_params={"name": body.username},
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
    old_data["role_ids"] = [
        link.role_id for link in target.roles
        if not getattr(link, "is_deleted", False)
    ] if target.roles else []
    old_username = target.username
    user = await update_user(db, user_id, body)
    result = build_user_item(user)
    new_data = orm_to_dict(user, "user")
    if body.role_ids is not None:
        new_data["role_ids"] = body.role_ids
    else:
        new_data["role_ids"] = [
            link.role_id for link in user.roles
            if not getattr(link, "is_deleted", False)
        ] if user.roles else []
    prev_val, new_val, raw_val = await prepare_log_values(db, "user", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_EDIT,
        operation_object_code="OBJ_USER", operation_object_params={"name": old_username},
        operation_content_code="LOG_USER_EDIT", operation_content_params={"name": old_username},
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
    old_data = orm_to_dict(target, "user")
    await reset_password(db, user_id, body.new_password, body.confirm_password)
    prev_val, _, raw_val = await prepare_log_values(db, "user", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_RESET_PWD,
        operation_object_code="OBJ_USER", operation_object_params={"name": target.username},
        operation_content_code="LOG_USER_RESET_PWD", operation_content_params={"name": target.username},
        ip_address=_get_ip(request),
        result="success",
        entity_type="user",
        entity_id=user_id,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
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
    prev_val, new_val, raw_val = await prepare_log_values(db, "user", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_STATUS,
        operation_object_code="OBJ_USER", operation_object_params={"name": target.username},
        operation_content_code="LOG_USER_STATUS_ENABLED" if body["status"] == "active" else "LOG_USER_STATUS_DISABLED",
        operation_content_params={"name": target.username},
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
    users = (
        await db.execute(
            select(UserModel).where(UserModel.id.in_(body.ids), UserModel.is_deleted.is_(False))
        )
    ).scalars().all()
    is_batch_delete = body.status == "deleted"
    # 启用/禁用跳过已处于目标状态的用户，日志仅记录实际发生变更的用户
    if not is_batch_delete:
        users = [u for u in users if u.status != body.status]
    if not users and not is_batch_delete:
        # 所选用户均已处于目标状态，无实际变更，不写日志
        return {"success": True, "updated": 0}
    # 快照必须在 batch_update_user_status 之前物化（该函数复用同一 session 的 ORM 对象，
    # 更新并 commit 后再读 u.status 拿到的已是目标状态）
    # 修复详情页操作历史缺失：此前批量只写一条日志且 entity_id=body.ids[0]，
    # 导致除首个用户外其他账号详情页查不到该操作；现改为每个用户单独一条、绑定各自 entity_id
    snapshots = [(u.id, u.username, u.status, orm_to_dict(u, "user")) for u in users]
    updated = await batch_update_user_status(db, [s[0] for s in snapshots], body.status)
    for uid, uname, prev_status, prev_dict in snapshots:
        if is_batch_delete:
            prev_json = json.dumps(prev_dict, ensure_ascii=False, default=_json_default)
            status_prev_json = prev_json
            status_updated_json = None
        else:
            # 启用/禁用记录各自的状态摘要，供详情页历史 Previous/Updated Value 展示真实前后状态
            status_prev_json = json.dumps({"status": prev_status}, ensure_ascii=False)
            status_updated_json = json.dumps({"status": body.status}, ensure_ascii=False)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.USER_BATCH_DELETE if is_batch_delete else OperationType.USER_BATCH_STATUS,
            operation_object_code="OBJ_USER", operation_object_params={"name": uname},
            operation_content_code=(
                "LOG_USER_BATCH_DELETE" if is_batch_delete
                else "LOG_USER_BATCH_ENABLE" if body.status == "active"
                else "LOG_USER_BATCH_DISABLE"
            ),
            operation_content_params={"names": uname},
            ip_address=_get_ip(request),
            result="success",
            entity_type="user",
            entity_id=uid,
            previous_value=status_prev_json,
            updated_value=status_updated_json,
            updated_value_json=(
                json.dumps({**prev_dict, "status": body.status}, ensure_ascii=False, default=_json_default)
                if not is_batch_delete
                else prev_json
            ),
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
        operation_object_code="OBJ_USER", operation_object_params={"name": target.username},
        operation_content_code="LOG_USER_DELETE", operation_content_params={"name": target.username},
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
    users = (await db.execute(select(UserModel).where(UserModel.id.in_(body.ids)))).scalars().all()
    # 快照在 batch_delete_users（内部会更新 ORM 对象并 commit）之前物化；
    # 每个用户单独写一条日志并绑定各自 entity_id，确保各账号详情页历史均可查询
    snapshots = [(u.id, u.username, orm_to_dict(u, "user")) for u in users]
    deleted = await batch_delete_users(db, body.ids)
    for uid, uname, prev_dict in snapshots:
        prev_json = json.dumps(prev_dict, ensure_ascii=False, default=_json_default)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.USER_BATCH_DELETE,
            operation_object_code="OBJ_USER", operation_object_params={"name": uname},
            # 模板占位符为 {names}（原代码误传 name，占位符不会被替换）
            operation_content_code="LOG_USER_BATCH_DELETE", operation_content_params={"names": uname},
            ip_address=_get_ip(request),
            result="success",
            entity_type="user",
            entity_id=uid,
            previous_value=prev_json,
            updated_value=None,
            updated_value_json=prev_json,
        )
    await db.commit()
    return {"success": True, "deleted": deleted}
