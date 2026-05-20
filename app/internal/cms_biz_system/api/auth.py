from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException, ForbiddenException, UnauthorizedException
from app.common.core.i18n import get_msg
from app.common.dependencies import get_db, get_current_user, _get_ip
from app.internal.cms_biz_system.models.user import Role, User, UserRole
from app.internal.cms_biz_system.schemas.user import CaptchaResponse, ChangePasswordRequest, LoginRequest, LoginResponse, UserInfo
from app.internal.cms_biz_system.services.captcha_service import create_captcha, verify_captcha
from app.internal.cms_biz_system.services.config_service import get_config_int
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_system.services.user_service import change_password

router = APIRouter(prefix="/auth")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def create_access_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.access_token_expire_minutes)
    return jwt.encode({**data, "exp": expire}, settings.secret_key, algorithm="HS256")


@router.get("/captcha", response_class=Response)
async def get_captcha(db: AsyncSession = Depends(get_db)):
    """
    获取验证码图片

    返回PNG格式的验证码图片，响应头包含 captcha-id
    """
    # 获取验证码过期时间配置（默认5分钟）
    expire_minutes = await get_config_int(db, "LOGIN_CAPTCHA_EXPIRE", 5)

    captcha_id, image_bytes = create_captcha(expire_minutes)

    # 在响应头中返回 captcha-id
    return Response(
        content=image_bytes,
        media_type="image/png",
        headers={"captcha-id": captcha_id},
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """
    用户登录

    验证流程：
    1. 校验验证码
    2. 检查账号是否锁定
    3. 校验用户名密码
    4. 检查是否需要强制修改密码
    """
    ip = _get_ip(request)

    # 1. 校验验证码
    if not verify_captcha(body.captcha_id, body.captcha_code):
        raise BusinessException(ErrorCode.CAPTCHA_INVALID, get_msg("CAPTCHA_INVALID"))

    # 获取登录失败相关配置
    max_fail_count = await get_config_int(db, "LOGIN_MAX_FAIL_COUNT", 5)
    lock_minutes = await get_config_int(db, "LOGIN_LOCK_MINUTES", 30)
    password_expire_days = await get_config_int(db, "PASSWORD_EXPIRE_DAYS", 90)

    # 查询用户
    result = await db.execute(
        select(User).where(User.username == body.username, User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()

    # 2. 检查账号是否锁定
    if user and user.locked_until:
        if datetime.now(timezone.utc) < user.locked_until:
            remaining = user.locked_until - datetime.now(timezone.utc)
            remaining_minutes = int(remaining.total_seconds() / 60) + 1
            raise ForbiddenException(ErrorCode.ACCOUNT_LOCKED_REMAINING, get_msg("ACCOUNT_LOCKED_REMAINING", remaining_minutes=remaining_minutes))
        else:
            # 锁定已过期，重置
            user.locked_until = None
            user.login_fail_count = 0

    # 3. 校验用户名密码
    if not user or not pwd_context.verify(body.password, user.password_hash):
        # 登录失败处理
        if user:
            user.login_fail_count = (user.login_fail_count or 0) + 1

            # 记录日志
            await write_log(
                db,
                user_id=user.id,
                user_name=user.username,
                operation_type=OperationType.USER_LOGIN,
                operation_object=user.username,
                operation_content=f"Login failed: invalid password (attempt {user.login_fail_count})",
                ip_address=ip,
                result="failed",
                error_message=get_msg("LOGIN_FAILED"),
            )

            # 检查是否需要锁定
            if user.login_fail_count >= max_fail_count:
                user.locked_until = datetime.now(timezone.utc) + timedelta(minutes=lock_minutes)
                await db.commit()
                raise ForbiddenException(ErrorCode.ACCOUNT_LOCKED_PERMANENT, get_msg("ACCOUNT_LOCKED_PERMANENT", max_fail_count=max_fail_count, lock_minutes=lock_minutes))

            await db.commit()

            # 提示剩余尝试次数
            remaining = max_fail_count - user.login_fail_count
            if remaining > 0:
                raise UnauthorizedException(ErrorCode.LOGIN_FAILED_WITH_REMAINING, get_msg("LOGIN_FAILED_WITH_REMAINING", remaining=remaining))

        # 用户不存在
        await write_log(
            db,
            user_id=None,
            user_name=body.username,
            operation_type=OperationType.USER_LOGIN,
            operation_object=body.username,
            operation_content=f"Login failed: user not found",
            ip_address=ip,
            result="failed",
            error_message=get_msg("LOGIN_FAILED"),
        )
        await db.commit()
        raise UnauthorizedException(ErrorCode.LOGIN_FAILED, get_msg("LOGIN_FAILED"))

    # 检查账号状态
    if user.status != "active":
        await write_log(
            db,
            user_id=user.id,
            user_name=user.username,
            operation_type=OperationType.USER_LOGIN,
            operation_object=user.username,
            operation_content=f"Login failed: account disabled",
            ip_address=ip,
            result="failed",
            error_message=get_msg("ACCOUNT_DISABLED"),
        )
        await db.commit()
        raise ForbiddenException(ErrorCode.ACCOUNT_DISABLED, get_msg("ACCOUNT_DISABLED"))

    # 登录成功，重置失败计数
    user.login_fail_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)

    # 检查是否需要强制修改密码
    force_change = False
    if user.force_change_password:
        force_change = True
    elif password_expire_days > 0 and user.password_changed_at:
        password_age = datetime.now(timezone.utc) - user.password_changed_at
        if password_age.days >= password_expire_days:
            force_change = True
            user.force_change_password = True

    # 记录成功日志
    await write_log(
        db,
        user_id=user.id,
        user_name=user.username,
        operation_type=OperationType.USER_LOGIN,
        operation_object=user.username,
        operation_content=f"User {user.username} logged in successfully",
        ip_address=ip,
        result="success",
    )
    await db.commit()

    token = create_access_token({"sub": user.username})
    return LoginResponse(
        access_token=token,
        display_name=user.display_name or user.username,
        username=user.username,
        force_change_password=force_change,
    )


@router.get("/me", response_model=UserInfo)
async def get_me(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
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
    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        status=current_user.status,
        role_codes=role_codes,
    )


@router.post("/logout")
async def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_LOGOUT,
        operation_object=current_user.username,
        operation_content=f"User {current_user.username} logged out",
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/session-timeout")
async def get_session_timeout(db: AsyncSession = Depends(get_db)):
    """获取会话空闲超时时间（分钟）"""
    timeout = await get_config_int(db, "SESSION_IDLE_TIMEOUT", 30)
    return {"value": timeout}


@router.post("/change-password")
async def change_password_endpoint(
    body: ChangePasswordRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    修改当前用户密码。

    验证规则：
    - 旧密码必须正确
    - 新密码与确认密码必须一致
    - 密码长度 >= 8 位
    - 密码必须包含大写、小写、数字、特殊符号中的至少3种
    - 禁止连续重复字符、键盘序列、常见单词等
    """
    await change_password(
        db,
        user_id=current_user.id,
        old_password=body.old_password,
        new_password=body.new_password,
        confirm_password=body.confirm_password,
        username=current_user.username,
    )

    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_CHANGE_PWD,
        operation_object=current_user.username,
        operation_content=f"User {current_user.username} changed password",
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()

    return {"success": True, "message": "密码修改成功"}
