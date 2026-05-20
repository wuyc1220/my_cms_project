"""数据权限管理 - API 路由"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
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
    clear_auth,
    get_roles_for_select,
    get_users_for_select,
    list_auth_contents,
)
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
    await authorize_contents(
        db, body.content_ids, body.role_ids, body.user_ids, created_by=current_user.id
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DATA_AUTH_AUTHORIZE,
        operation_object=f"内容 {body.content_ids}",
        operation_content=f"授权给角色 {body.role_ids} 和用户 {body.user_ids}",
        ip_address=_get_ip(request),
    )
    await db.commit()
    return {"message": "ok"}


@router.post("/clear")
async def clear(
    body: ContentAuthClearPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """清除授权"""
    await clear_auth(db, body.content_ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DATA_AUTH_CLEAR,
        operation_object=f"内容 {body.content_ids}",
        operation_content=f"清除数据权限",
        ip_address=_get_ip(request),
    )
    await db.commit()
    return {"message": "ok"}


@router.post("/batch-authorize")
async def batch_authorize(
    body: ContentAuthAuthorizePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量授权"""
    await authorize_contents(
        db, body.content_ids, body.role_ids, body.user_ids, created_by=current_user.id
    )
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DATA_AUTH_AUTHORIZE,
        operation_object=f"内容 {body.content_ids}",
        operation_content=f"批量授权给角色 {body.role_ids} 和用户 {body.user_ids}",
        ip_address=_get_ip(request),
    )
    await db.commit()
    return {"message": "ok"}


@router.post("/batch-clear")
async def batch_clear(
    body: ContentAuthClearPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量清除授权"""
    await clear_auth(db, body.content_ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DATA_AUTH_CLEAR,
        operation_object=f"内容 {body.content_ids}",
        operation_content=f"批量清除数据权限",
        ip_address=_get_ip(request),
        entity_type="content_auth",
    )
    await db.commit()
    return {"message": "ok"}


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
