from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException, ForbiddenException, UnauthorizedException
from app.common.core.i18n import get_msg
from app.common.dependencies import get_db, get_current_user, _get_ip, get_cache_service
from app.internal.cms_biz_system.models.user import Role, User, UserRole
from app.internal.cms_biz_system.schemas.user import CaptchaResponse, ChangePasswordRequest, LoginRequest, LoginResponse, UserInfo
from app.internal.cms_biz_system.services.captcha_service import create_captcha, verify_captcha
from app.internal.cms_biz_system.services.config_service import get_config_int
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_system.services.user_service import change_password

from app.common.services.cache_service import CacheService


router = APIRouter(prefix="/auth")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_SESSION_KEY_PREFIX = "session:user:"


def create_access_token(data: dict, expire_minutes: int | None = None) -> str:
    """
    创建 JWT Token

    Args:
        data: Token 数据（需包含 sub 和 jti）
        expire_minutes: 过期时间（分钟），优先使用数据库配置
    """
    expire_min = expire_minutes if expire_minutes is not None else settings.access_token_expire_minutes
    expire = datetime.now(timezone.utc) + timedelta(minutes=expire_min)
    return jwt.encode({**data, "exp": expire}, settings.secret_key, algorithm="HS256")


async def _get_session_key(user_id: int) -> str:
    return f"{_SESSION_KEY_PREFIX}{user_id}"


async def _load_sessions(db: AsyncSession, cache: CacheService, user_id: int) -> list[dict]:
    """加载用户的活跃会话列表"""
    data = await cache.get(db, await _get_session_key(user_id))
    if not data:
        return []
    return data.get("sessions", [])


async def _save_sessions(
    db: AsyncSession,
    cache: CacheService,
    user_id: int,
    sessions: list[dict],
    ttl_seconds: int,
) -> None:
    """保存用户的活跃会话列表"""
    await cache.set(
        db,
        await _get_session_key(user_id),
        {"sessions": sessions},
        ttl_seconds=ttl_seconds,
    )


async def _add_session(
    db: AsyncSession,
    cache: CacheService,
    user_id: int,
    jti: str,
    ip: str | None,
    max_sessions: int,
    ttl_seconds: int,
) -> None:
    """添加新会话，超过最大数量时踢掉最早的会话。

    使用 PG 事务级 advisory lock 序列化同一用户的会话读-改-写，
    避免并发登录互相覆盖 sessions 列表导致会话凭空丢失（偶现被踢下线）。
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(95001, :uid)"),
        {"uid": user_id},
    )
    sessions = await _load_sessions(db, cache, user_id)

    # 移除已存在的相同 jti（理论上不会有，但防御性处理）
    sessions = [s for s in sessions if s["jti"] != jti]

    # 添加新会话到末尾（最新）
    sessions.append({
        "jti": jti,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ip": ip or "",
    })

    # 超过限制时，踢掉最早的
    while len(sessions) > max_sessions:
        kicked = sessions.pop(0)
        logger.warning(
            f"[Auth] 会话数超限踢出最早会话 | user_id={user_id} "
            f"max_sessions={max_sessions} kicked_jti={kicked.get('jti')} "
            f"kicked_ip={kicked.get('ip')} kicked_created_at={kicked.get('created_at')}"
        )

    await _save_sessions(db, cache, user_id, sessions, ttl_seconds)


async def _remove_session(
    db: AsyncSession,
    cache: CacheService,
    user_id: int,
    jti: str,
    ttl_seconds: int,
) -> None:
    """移除指定会话（登出时使用）。

    同样使用 advisory lock，避免与并发登录的写回互相覆盖。
    """
    await db.execute(
        text("SELECT pg_advisory_xact_lock(95001, :uid)"),
        {"uid": user_id},
    )
    sessions = await _load_sessions(db, cache, user_id)
    sessions = [s for s in sessions if s["jti"] != jti]
    if sessions:
        await _save_sessions(db, cache, user_id, sessions, ttl_seconds)
    else:
        await cache.delete(db, await _get_session_key(user_id))


async def _clear_all_sessions(
    db: AsyncSession,
    cache: CacheService,
    user_id: int,
) -> None:
    """清除用户所有会话（改密码、管理员踢人时使用）"""
    await cache.delete(db, await _get_session_key(user_id))


async def _validate_session(
    db: AsyncSession,
    cache: CacheService,
    user_id: int,
    jti: str,
) -> bool:
    """校验会话是否有效（jti 是否在活跃会话列表中）"""
    sessions = await _load_sessions(db, cache, user_id)
    return any(s["jti"] == jti for s in sessions)


@router.get("/captcha", response_class=Response)
async def get_captcha(request: Request, db: AsyncSession = Depends(get_db), cache: CacheService = Depends(get_cache_service)):
    """
    获取验证码图片

    返回PNG格式的验证码图片，响应头包含 captcha-id
    当请求携带 cookie cms_test_mode=1 时，返回固定验证码（用于自动化测试）
    """
    expire_minutes = await get_config_int(db, "LOGIN_CAPTCHA_EXPIRE", 5)
    test_mode = request.cookies.get("cms_test_mode") == "1"

    captcha_id, image_bytes = await create_captcha(db, cache, expire_minutes, test_mode=test_mode)
    await db.commit()

    return Response(
        content=image_bytes,
        media_type="image/png",
        headers={"captcha-id": captcha_id},
    )


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, request: Request, db: AsyncSession = Depends(get_db), cache: CacheService = Depends(get_cache_service)):
    """
    用户登录

    验证流程：
    1. 校验验证码
    2. 检查账号是否锁定
    3. 校验用户名密码
    4. 检查是否需要强制修改密码
    5. 写入会话缓存（支持多设备登录，有数量限制）
    """
    ip = _get_ip(request)

    # 1. 校验验证码
    if not await verify_captcha(db, cache, body.captcha_id, body.captcha_code):
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
                operation_content_code="LOG_LOGIN_FAILED_INVALID_PASSWORD", operation_content_params={"count": user.login_fail_count},
                ip_address=ip,
                result="failed",
                error_message_code="LOGIN_FAILED",
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
            operation_content_code="LOG_LOGIN_FAILED_USER_NOT_FOUND",
            ip_address=ip,
            result="failed",
            error_message_code="LOGIN_FAILED",
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
            operation_content_code="LOG_LOGIN_FAILED_ACCOUNT_DISABLED",
            ip_address=ip,
            result="failed",
            error_message_code="ACCOUNT_DISABLED",
        )
        await db.commit()
        raise ForbiddenException(ErrorCode.ACCOUNT_DISABLED, get_msg("ACCOUNT_DISABLED"))

    # 登录成功，重置失败计数
    user.login_fail_count = 0
    user.locked_until = None
    user.last_login_at = datetime.now(timezone.utc)

    # 检查密码是否过期（password_changed_at 为空则不受密码过期管控）
    force_change = False
    if password_expire_days > 0 and user.password_changed_at:
        password_age = datetime.now(timezone.utc) - user.password_changed_at
        if password_age.days >= password_expire_days:
            force_change = True

    # 记录成功日志
    await write_log(
        db,
        user_id=user.id,
        user_name=user.username,
        operation_type=OperationType.USER_LOGIN,
        operation_object=user.username,
        operation_content_code="LOG_USER_LOGIN", operation_content_params={"name": user.username},
        ip_address=ip,
        result="success",
    )
    await db.commit()

    # 从数据库读取 Token 有效期配置
    token_expire = await get_config_int(db, "ACCESS_TOKEN_EXPIRE_MINUTES", default_value=settings.access_token_expire_minutes)
    max_sessions = await get_config_int(db, "MAX_CONCURRENT_SESSIONS", default_value=5)

    # 生成 jti 并创建 token
    jti = str(uuid4())
    token = create_access_token({"sub": user.username, "jti": jti}, expire_minutes=token_expire)

    # 写入会话缓存（支持多设备，超过限制踢最早的）
    await _add_session(
        db,
        cache,
        user.id,
        jti,
        ip,
        max_sessions=max_sessions,
        ttl_seconds=token_expire * 60,
    )
    await db.commit()

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
            UserRole.is_deleted.is_(False),
            Role.is_deleted.is_(False),
            Role.status == "active",
        )
    )
    role_codes = list(codes_result.scalars().all())
    password_expire_days = await get_config_int(db, "PASSWORD_EXPIRE_DAYS", 90)
    force_change = False
    if password_expire_days > 0 and current_user.password_changed_at:
        password_age = datetime.now(timezone.utc) - current_user.password_changed_at
        if password_age.days >= password_expire_days:
            force_change = True

    return UserInfo(
        id=current_user.id,
        username=current_user.username,
        display_name=current_user.display_name,
        status=current_user.status,
        role_codes=role_codes,
        force_change_password=force_change,
    )


@router.post("/logout")
async def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    cache: CacheService = Depends(get_cache_service),
):
    """登出：从缓存中移除当前会话"""
    # 从 Authorization 头提取 jti
    auth_header = request.headers.get("authorization", "")
    jti = None
    if auth_header.startswith("Bearer "):
        try:
            payload = jwt.decode(auth_header[7:], settings.secret_key, algorithms=["HS256"])
            jti = payload.get("jti")
            exp = payload.get("exp")
            # 计算token剩余有效时间（秒）
            remaining_seconds = (exp - datetime.now(timezone.utc).timestamp()) if exp else None
            logger.info(
                f"[Logout] 主动登出 | user_id={current_user.id} username={current_user.username} "
                f"jti={jti} token_remaining_seconds={remaining_seconds} "
                f"ip={_get_ip(request)} "
                f"referer={request.headers.get('referer', '')} "
                f"user_agent={request.headers.get('user-agent', '')}"
            )
        except JWTError:
            logger.warning(f"[Logout] JWT解码失败 | user_id={current_user.id} username={current_user.username}")

    if jti:
        token_expire = await get_config_int(db, "ACCESS_TOKEN_EXPIRE_MINUTES", default_value=settings.access_token_expire_minutes)
        await _remove_session(db, cache, current_user.id, jti, ttl_seconds=token_expire * 60)
        await db.commit()

    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_LOGOUT,
        operation_object=current_user.username,
        operation_content_code="LOG_USER_LOGOUT", operation_content_params={"name": current_user.username},
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
    cache: CacheService = Depends(get_cache_service),
):
    """
    修改当前用户密码。

    验证规则：
    - 旧密码必须正确
    - 新密码与确认密码必须一致
    - 密码长度 >= 8 位
    - 密码必须包含大写、小写、数字、特殊符号中的至少3种
    - 禁止连续重复字符、键盘序列等

    修改成功后清除所有会话，强制所有设备重新登录。
    """
    await change_password(
        db,
        user_id=current_user.id,
        old_password=body.old_password,
        new_password=body.new_password,
        confirm_password=body.confirm_password,
        username=current_user.username,
    )

    # 清除所有会话，强制所有设备重新登录
    await _clear_all_sessions(db, cache, current_user.id)
    await db.commit()

    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.USER_CHANGE_PWD,
        operation_object=current_user.username,
        operation_content_code="LOG_USER_CHANGE_PWD", operation_content_params={"name": current_user.username},
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()

    return {"success": True, "message": get_msg("PASSWORD_CHANGE_SUCCESS")}
