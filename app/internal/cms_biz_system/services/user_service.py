"""
用户业务逻辑层
只负责业务规则校验和流程编排，数据访问委托给 repository
"""
from datetime import datetime, timezone

from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_system.models.user import Role, User
from app.internal.cms_biz_system.repositories import user_repository
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.schemas.user_crud import RoleListItem, UserCreate, UserListItem, UserUpdate
from loguru import logger
from app.common.core.i18n import get_msg
from app.common.core.exceptions import ErrorCode, BusinessException

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── 辅助函数 ────────────────────────────────────────────────

def build_user_item(user: User) -> UserListItem:
    """ORM → Schema 转换"""
    return UserListItem(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        phone_number=user.phone_number,
        status=user.status,
        created_at=user.created_at,
        roles=[
            RoleListItem.model_validate(link.role)
            for link in user.roles
            if getattr(link, "role", None) is not None and not link.role.is_deleted
        ],
    )


async def _ensure_roles_exist(db: AsyncSession, role_ids: list[int]) -> list[Role]:
    """校验角色ID全部存在，不存在则抛异常"""
    roles = await user_repository.get_roles_by_ids(db, role_ids)
    if len(roles) != len(set(role_ids)):
        raise BusinessException(ErrorCode.USER_ROLE_NOT_FOUND, get_msg("USER_ROLE_NOT_FOUND"))
    return roles


async def _get_user_or_404(db: AsyncSession, user_id: int) -> User:
    """获取用户，不存在则404"""
    user = await user_repository.get_user_by_id(db, user_id)
    if not user:
        raise BusinessException(ErrorCode.USER_NOT_FOUND, get_msg("USER_NOT_FOUND"))
    return user


async def _assert_user_not_in_use(db: AsyncSession, user: User) -> None:
    """删除前校验用户是否被引用：被数据权限授权或存在未完成任务分配时禁止删除"""
    # 局部导入避免模块级循环依赖
    from sqlalchemy import select
    from app.internal.cms_biz_system.models.content_auth import ContentAuth
    from app.internal.cms_biz_package.models.task import Task

    name = user.display_name or user.username
    in_auth = (await db.execute(
        select(ContentAuth.id).where(
            ContentAuth.user_id == user.id, ContentAuth.is_deleted.is_(False)
        ).limit(1)
    )).scalar_one_or_none()
    if in_auth:
        raise BusinessException(ErrorCode.USER_AUTH_IN_USE, get_msg("USER_AUTH_IN_USE", name=name))
    pending_task = (await db.execute(
        select(Task.id).where(
            Task.assignee_id == user.id,
            Task.is_deleted.is_(False),
            Task.task_status != "Completed",
        ).limit(1)
    )).scalar_one_or_none()
    if pending_task:
        raise BusinessException(ErrorCode.USER_HAS_ASSIGNED_TASKS, get_msg("USER_HAS_ASSIGNED_TASKS", name=name))


# ── 用户 CRUD ───────────────────────────────────────────────

async def list_users(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    username: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    phone_number: str | None = None,
    status: str | None = None,
    role_ids: list[int] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[UserListItem]:
    logger.info(f"list_users 入参: page={page}, page_size={page_size}, username={username}, display_name={display_name}, email={email}, phone_number={phone_number}, status={status}, role_ids={role_ids}, sort_by={sort_by}, sort_order={sort_order}")
    users, total = await user_repository.list_users_query(
        db,
        page=page, page_size=page_size,
        username=username, display_name=display_name,
        email=email, phone_number=phone_number,
        status=status, role_ids=role_ids,
        sort_by=sort_by, sort_order=sort_order,
    )
    return PaginatedResponse(
        total=total, page=page, page_size=page_size,
        items=[build_user_item(u) for u in users],
    )


async def get_user(db: AsyncSession, user_id: int) -> User:
    logger.info(f"get_user 入参: user_id={user_id}")
    return await _get_user_or_404(db, user_id)


async def create_user(db: AsyncSession, data: UserCreate) -> User:
    existing = await user_repository.get_user_by_username(db, data.username)
    logger.info(f"create_user 入参: data={data}")
    if existing:
        raise BusinessException(ErrorCode.USERNAME_EXISTS, get_msg("USERNAME_EXISTS"))

    from app.internal.cms_biz_system.services.config_service import get_config_int
    from app.internal.cms_biz_system.services.password_validator import validate_password

    min_length = await get_config_int(db, "PASSWORD_MIN_LENGTH", 8)
    pattern_min_len = await get_config_int(db, "PASSWORD_PATTERN_MIN_LEN", 6)
    is_valid, errors = validate_password(data.password, data.username, min_length, pattern_min_len)
    if not is_valid:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, "; ".join(errors))

    user = User(
        username=data.username,
        password_hash=pwd_context.hash(data.password),
        display_name=data.display_name,
        email=data.email,
        phone_number=data.phone_number,
        status=data.status,
        password_changed_at=datetime.now(timezone.utc),
    )
    await user_repository.add_user(db, user)

    roles = await _ensure_roles_exist(db, data.role_ids)
    for role in roles:
        await user_repository.add_user_role(db, user.id, role.id)

    await db.commit()
    return await _get_user_or_404(db, user.id)


async def update_user(db: AsyncSession, user_id: int, data: UserUpdate) -> User:
    user = await _get_user_or_404(db, user_id)
    logger.info(f"update_user 入参: user_id={user_id}, data={data}")
    if data.display_name is not None:
        user.display_name = data.display_name
    if data.email is not None:
        user.email = data.email
    if data.phone_number is not None:
        user.phone_number = data.phone_number
    if data.status is not None:
        user.status = data.status
    if data.role_ids is not None:
        await user_repository.delete_user_roles(db, user_id)
        await _ensure_roles_exist(db, data.role_ids)
        for role_id in data.role_ids:
            await user_repository.add_user_role(db, user_id, role_id)

    await db.commit()
    return await _get_user_or_404(db, user_id)


async def reset_password(
    db: AsyncSession,
    user_id: int,
    new_password: str,
    confirm_password: str,
) -> None:
    from app.internal.cms_biz_system.services.config_service import get_config_int
    from app.internal.cms_biz_system.services.password_validator import validate_password

    user = await _get_user_or_404(db, user_id)
    logger.info(f"reset_password 入参: user_id={user_id}")

    if new_password != confirm_password:
        raise BusinessException(ErrorCode.PASSWORDS_DO_NOT_MATCH, get_msg("PASSWORDS_DO_NOT_MATCH"))

    min_length = await get_config_int(db, "PASSWORD_MIN_LENGTH", 8)
    pattern_min_len = await get_config_int(db, "PASSWORD_PATTERN_MIN_LEN", 6)
    is_valid, errors = validate_password(new_password, user.username, min_length, pattern_min_len)
    if not is_valid:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, "; ".join(errors))

    if pwd_context.verify(new_password, user.password_hash):
        raise BusinessException(ErrorCode.SAME_AS_OLD_PASSWORD, get_msg("SAME_AS_OLD_PASSWORD"))

    user.password_hash = pwd_context.hash(new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()


async def toggle_user_status(db: AsyncSession, user_id: int, new_status: str) -> User:
    user = await _get_user_or_404(db, user_id)
    logger.info(f"toggle_user_status 入参: user_id={user_id}, new_status={new_status}")
    user.status = new_status
    if new_status == "active":
        user.locked_until = None
        user.login_fail_count = 0
    await db.commit()
    return await _get_user_or_404(db, user_id)


async def batch_update_user_status(db: AsyncSession, ids: list[int], new_status: str) -> int:
    users = await user_repository.get_users_by_ids(db, ids)
    logger.info(f"batch_update_user_status 入参: ids={ids}, new_status={new_status}")
    # 启用/禁用时跳过已处于目标状态的用户（批量启用跳过已启用、批量禁用跳过已禁用）
    if new_status != "deleted":
        users = [u for u in users if u.status != new_status]
    for user in users:
        if new_status == "deleted":
            await _assert_user_not_in_use(db, user)
            user.is_deleted = True
        else:
            user.status = new_status
            if new_status == "active":
                user.locked_until = None
                user.login_fail_count = 0
    await db.commit()
    return len(users)


async def delete_user(db: AsyncSession, user_id: int) -> None:
    user = await _get_user_or_404(db, user_id)
    logger.info(f"delete_user 入参: user_id={user_id}")
    await _assert_user_not_in_use(db, user)
    user.is_deleted = True
    await db.commit()


async def batch_delete_users(db: AsyncSession, ids: list[int]) -> int:
    users = await user_repository.get_users_by_ids(db, ids)
    logger.info(f"batch_delete_users 入参: ids={ids}")
    for user in users:
        await _assert_user_not_in_use(db, user)
        user.is_deleted = True
    await db.commit()
    return len(users)


async def change_password(
    db: AsyncSession,
    user_id: int,
    old_password: str,
    new_password: str,
    confirm_password: str,
    username: str,
) -> None:
    logger.info(f"change_password 入参: user_id={user_id}, username={username}")
    from app.internal.cms_biz_system.services.config_service import get_config_int
    from app.internal.cms_biz_system.services.password_validator import validate_password

    user = await _get_user_or_404(db, user_id)

    min_length = await get_config_int(db, "PASSWORD_MIN_LENGTH", 8)
    pattern_min_len = await get_config_int(db, "PASSWORD_PATTERN_MIN_LEN", 6)

    if not pwd_context.verify(old_password, user.password_hash):
        raise BusinessException(ErrorCode.OLD_PASSWORD_INCORRECT, get_msg("OLD_PASSWORD_INCORRECT"))
    if new_password != confirm_password:
        raise BusinessException(ErrorCode.PASSWORDS_DO_NOT_MATCH, get_msg("PASSWORDS_DO_NOT_MATCH"))

    is_valid, errors = validate_password(new_password, username, min_length, pattern_min_len)
    if not is_valid:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, "; ".join(errors))
    if pwd_context.verify(new_password, user.password_hash):
        raise BusinessException(ErrorCode.SAME_AS_OLD_PASSWORD, get_msg("SAME_AS_OLD_PASSWORD"))

    user.password_hash = pwd_context.hash(new_password)
    user.password_changed_at = datetime.now(timezone.utc)
    await db.commit()
