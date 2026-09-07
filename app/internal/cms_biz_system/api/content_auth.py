"""数据权限管理 - API 路由"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.common.utils.log_enricher import prepare_log_values
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.content_auth import (
    ContentAuthAuthorizePayload,
    ContentAuthClearPayload,
    RoleSimpleItem,
    UserSimpleItem,
)
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.schemas.content_auth import ContentAuthListItem
from app.internal.cms_biz_system.services.content_auth_service import (
    authorize_contents,
    check_content_auth_permission,
    clear_auth,
    get_auth_snapshot_map,
    get_roles_for_select,
    get_users_for_select,
    list_auth_contents,
)
from app.common.core.i18n import get_msg
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log

router = APIRouter(prefix="/data-authorization")


@router.get("/contents", response_model=PaginatedResponse[ContentAuthListItem])
async def get_auth_contents(
    page: int = 1,
    page_size: int = 10,
    content_name: str | None = None,
    content_types: str | None = None,
    ingest_statuses: str | None = None,
    authorized_user: str | None = None,
    authorized_role: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """获取内容列表（带授权信息）"""
    # 处理逗号分隔的多选值
    ct_list = content_types.split(",") if content_types else None
    is_list = ingest_statuses.split(",") if ingest_statuses else None
    return await list_auth_contents(
        db, page, page_size, content_name, ct_list, is_list, authorized_user, authorized_role, sort_by, sort_order
    )


@router.post("/authorize")
async def authorize(
    body: ContentAuthAuthorizePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """授权（覆盖式）"""
    old_snapshot = await get_auth_snapshot_map(db, body.content_ids)
    await authorize_contents(
        db, body.content_ids, body.role_ids, body.user_ids, created_by=current_user.id
    )
    new_snapshot = await get_auth_snapshot_map(db, body.content_ids)
    for cid in body.content_ids:
        old_data = {"content_id": cid, **old_snapshot[cid]}
        new_data = {"content_id": cid, **new_snapshot[cid]}
        prev_val, new_val, raw_val = await prepare_log_values(db, "content_auth", old_data, new_data)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.DATA_AUTH_AUTHORIZE,
            operation_object_code="OBJ_CONTENT", operation_object_params={"name": cid},
            operation_content_code="log.dataAuth.authorize",
            content_id=cid,
            entity_type="content_auth",
            entity_id=cid,
            previous_value=prev_val,
            updated_value=new_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
        )
    await db.commit()
    return {"message": get_msg("OPERATION_SUCCESS")}


@router.post("/clear")
async def clear(
    body: ContentAuthClearPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """清除授权"""
    old_snapshot = await get_auth_snapshot_map(db, body.content_ids)
    await clear_auth(db, body.content_ids)
    for cid in body.content_ids:
        old_data = {"content_id": cid, **old_snapshot[cid]}
        prev_val, _, raw_val = await prepare_log_values(db, "content_auth", old_data, None)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.DATA_AUTH_CLEAR,
            operation_object_code="OBJ_CONTENT", operation_object_params={"name": cid},
            operation_content_code="log.dataAuth.clear",
            content_id=cid,
            entity_type="content_auth",
            entity_id=cid,
            previous_value=prev_val,
            updated_value=None,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
        )
    await db.commit()
    return {"message": get_msg("OPERATION_SUCCESS")}


@router.post("/batch-authorize")
async def batch_authorize(
    body: ContentAuthAuthorizePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量授权"""
    old_snapshot = await get_auth_snapshot_map(db, body.content_ids)
    await authorize_contents(
        db, body.content_ids, body.role_ids, body.user_ids, created_by=current_user.id
    )
    new_snapshot = await get_auth_snapshot_map(db, body.content_ids)
    for cid in body.content_ids:
        old_data = {"content_id": cid, **old_snapshot[cid]}
        new_data = {"content_id": cid, **new_snapshot[cid]}
        prev_val, new_val, raw_val = await prepare_log_values(db, "content_auth", old_data, new_data)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.DATA_AUTH_AUTHORIZE,
            operation_object_code="OBJ_CONTENT", operation_object_params={"name": cid},
            operation_content_code="log.dataAuth.authorize",
            content_id=cid,
            entity_type="content_auth",
            entity_id=cid,
            previous_value=prev_val,
            updated_value=new_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
        )
    await db.commit()
    return {"message": get_msg("OPERATION_SUCCESS")}


@router.post("/batch-clear")
async def batch_clear(
    body: ContentAuthClearPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量清除授权"""
    old_snapshot = await get_auth_snapshot_map(db, body.content_ids)
    await clear_auth(db, body.content_ids)
    for cid in body.content_ids:
        old_data = {"content_id": cid, **old_snapshot[cid]}
        prev_val, _, raw_val = await prepare_log_values(db, "content_auth", old_data, None)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.DATA_AUTH_CLEAR,
            operation_object_code="OBJ_CONTENT", operation_object_params={"name": cid},
            operation_content_code="log.dataAuth.clear",
            content_id=cid,
            entity_type="content_auth",
            entity_id=cid,
            previous_value=prev_val,
            updated_value=None,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
        )
    await db.commit()
    return {"message": get_msg("OPERATION_SUCCESS")}


@router.get("/roles", response_model=list[RoleSimpleItem])
async def get_roles(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """角色下拉列表"""
    return await get_roles_for_select(db)


@router.get("/users", response_model=list[UserSimpleItem])
async def get_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """用户下拉列表"""
    return await get_users_for_select(db)


@router.get("/check-permission/{content_id}")
async def check_permission(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """检查当前用户是否有访问指定内容的权限
    
    用于前端点击列表跳转到详情前进行权限校验
    """
    has_permission = await check_content_auth_permission(
        db, current_user.id, content_id
    )
    return {
        "has_permission": has_permission,
        "content_id": content_id,
        "user_id": current_user.id,
    }
