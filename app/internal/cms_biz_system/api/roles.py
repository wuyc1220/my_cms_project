import json

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
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict, _json_default
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
        operation_object_code="OBJ_ROLE", operation_object_params={"name": role.name},
        operation_content_code="LOG_ROLE_CREATE", operation_content_params={"name": role.name},
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
        operation_object_code="OBJ_ROLE", operation_object_params={"name": role.name},
        operation_content_code="LOG_ROLE_EDIT", operation_content_params={"name": role.name},
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
        operation_object_code="OBJ_ROLE", operation_object_params={"name": role_name},
        operation_content_code="LOG_ROLE_DELETE", operation_content_params={"name": role_name},
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
    prev_val, new_val, raw_val = await prepare_log_values(db, "role", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.ROLE_STATUS,
        operation_object_code="OBJ_ROLE", operation_object_params={"name": role.name},
        operation_content_code="LOG_ROLE_STATUS_ENABLED" if body["status"] == "active" else "LOG_ROLE_STATUS_DISABLED",
        operation_content_params={"name": role.name},
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
    roles = (await db.execute(select(Role).where(Role.id.in_(body.ids)))).scalars().all()
    is_batch_delete = body.status == "deleted"
    # 快照必须在 batch_update_role_status 之前物化（该函数复用同一 session 的 ORM 对象，
    # 更新并 commit 后再读 r.status 拿到的已是目标状态）
    # 修复详情页操作历史缺失：此前批量只写一条日志且 entity_id=body.ids[0]，
    # 导致除首个角色外其他角色详情页查不到该操作；现改为每个角色单独一条、绑定各自 entity_id
    snapshots = [(r.id, r.name, r.status, orm_to_dict(r)) for r in roles]
    updated = await batch_update_role_status(db, [s[0] for s in snapshots], body.status)
    for rid, rname, prev_status, prev_dict in snapshots:
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
            operation_type=OperationType.ROLE_BATCH_DELETE if is_batch_delete else OperationType.ROLE_BATCH_STATUS,
            operation_object_code="OBJ_ROLE", operation_object_params={"name": rname},
            operation_content_code=(
                "LOG_ROLE_BATCH_DELETE" if is_batch_delete
                else "LOG_ROLE_BATCH_ENABLE" if body.status == "active"
                else "LOG_ROLE_BATCH_DISABLE"
            ),
            operation_content_params={"names": rname},
            ip_address=_get_ip(request),
            result="success",
            entity_type="role",
            entity_id=rid,
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


@router.post("/batch-delete")
async def batch_delete_role_api(
    body: BatchIdsRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    roles = (await db.execute(select(Role).where(Role.id.in_(body.ids)))).scalars().all()
    # 快照在 batch_delete_roles（内部会更新 ORM 对象并 commit）之前物化；
    # 每个角色单独写一条日志并绑定各自 entity_id，确保各角色详情页历史均可查询
    snapshots = [(r.id, r.name, orm_to_dict(r)) for r in roles]
    deleted = await batch_delete_roles(db, body.ids)
    for rid, rname, prev_dict in snapshots:
        prev_json = json.dumps(prev_dict, ensure_ascii=False, default=_json_default)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.ROLE_BATCH_DELETE,
            operation_object_code="OBJ_ROLE", operation_object_params={"name": rname},
            operation_content_code="LOG_ROLE_BATCH_DELETE", operation_content_params={"names": rname},
            ip_address=_get_ip(request),
            result="success",
            entity_type="role",
            entity_id=rid,
            previous_value=prev_json,
            updated_value=None,
            updated_value_json=prev_json,
        )
    await db.commit()
    return {"success": True, "deleted": deleted}
