"""
公共依赖
提供 get_db / get_current_user / _get_ip / get_cache_service 等全局依赖注入
"""
from typing import AsyncGenerator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.common.core.i18n import get_msg
from app.common.core.exceptions import ErrorCode, ForbiddenException, UnauthorizedException
from app.common.services.cache_service import CacheService
from app.database import AsyncSessionLocal, set_current_user_id
from app.internal.cms_biz_system.models.user import User

security = HTTPBearer(auto_error=False)

_cache_service: CacheService | None = None


# ── 数据库会话 ──────────────────────────────────────────────

async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """获取数据库会话"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            set_current_user_id(None)
            await session.close()


# ── 认证 ────────────────────────────────────────────────────

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """解析JWT令牌，返回当前用户，同时校验 jti 是否在活跃会话列表中"""
    if not credentials:
        logger.warning("[Auth] 无 credentials，请求未携带 Token")
        raise UnauthorizedException(ErrorCode.INVALID_TOKEN, get_msg("INVALID_TOKEN"))

    token = credentials.credentials
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        username: str = payload.get("sub")
        jti: str = payload.get("jti")
        exp = payload.get("exp")
        if not username:
            logger.warning(f"[Auth] Token payload 无 username | jti={jti} exp={exp}")
            raise UnauthorizedException(ErrorCode.INVALID_TOKEN, get_msg("INVALID_TOKEN"))
    except JWTError as e:
        logger.warning(f"[Auth] JWT 解码失败 | error={type(e).__name__}: {e} | token_prefix={token[:20] if token else 'None'}")
        raise UnauthorizedException(ErrorCode.INVALID_TOKEN, get_msg("INVALID_TOKEN"))

    result = await db.execute(
        select(User).where(User.username == username, User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    if not user:
        logger.warning(f"[Auth] 用户不存在 | username={username}")
        raise UnauthorizedException(ErrorCode.USER_NOT_FOUND, get_msg("USER_NOT_FOUND"))

    # 校验用户状态：禁用/锁定等非 active 状态的用户立即踢出（返回 401 触发前端跳转登录页）
    if user.status != "active":
        logger.warning(f"[Auth] 用户状态非 active | username={username} status={user.status}")
        raise UnauthorizedException(ErrorCode.ACCOUNT_DISABLED, get_msg("ACCOUNT_DISABLED"))

    # 校验 jti 是否在活跃会话列表中（支持多设备登录管理）
    if jti:
        cache = get_cache_service()
        session_data = await cache.get(db, f"session:user:{user.id}")
        if session_data:
            sessions = session_data.get("sessions", [])
            active_jtis = [s.get("jti") for s in sessions]
            if not any(s["jti"] == jti for s in sessions):
                logger.warning(
                    f"[Auth] 会话失效 | user_id={user.id} username={username} "
                    f"jti={jti} active_jtis={active_jtis} sessions_count={len(sessions)}"
                )
                raise UnauthorizedException(ErrorCode.SESSION_INVALID, get_msg("SESSION_INVALID"))
        else:
            logger.warning(
                f"[Auth] 会话缓存为空 | user_id={user.id} username={username} jti={jti}"
            )
            raise UnauthorizedException(ErrorCode.SESSION_INVALID, get_msg("SESSION_INVALID"))

    set_current_user_id(user.id)
    return user


# ── 工具函数 ─────────────────────────────────────────────────

def _get_ip(request: Request) -> str | None:
    """从请求中获取客户端真实IP（支持反向代理）"""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    return request.client.host if request.client else None


# ── 权限检查 ────────────────────────────────────────────────

# 预置 admin 角色 code（比较时统一大写）
_ADMIN_ROLE_CODE = "ADMIN"


def require_permission(permission: str):
    """权限检查依赖：校验当前用户的有效角色是否绑定了指定权限菜单。

    校验规则：
    - 用户任一有效角色（未删除且 status='active'）绑定的权限菜单
      （menu_type='permission'）中包含 permission 对应的 i18n_key 即放行；
    - 预置 admin 角色（忽略大小写）直接放行；
    - 校验失败抛出 ForbiddenException（403）。

    注意：menu_type='permission' 必须放在 outerjoin 条件中而不是 where，
    否则角色未绑定任何权限菜单时（如预置 ADMIN 角色）rows 为空，
    admin 放行判断永远不生效，导致 admin 被误判 403。
    """
    async def permission_checker(
        user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        # 延迟导入避免循环依赖（models 层可能反向引用公共依赖）
        from app.internal.cms_biz_system.models.menu import Menu, RoleMenu
        from app.internal.cms_biz_system.models.user import Role, UserRole

        result = await db.execute(
            select(Role.code, Menu.i18n_key)
            .join(UserRole, UserRole.role_id == Role.id)
            .outerjoin(RoleMenu, (RoleMenu.role_id == Role.id) & (RoleMenu.is_deleted.is_(False)))
            .outerjoin(
                Menu,
                (Menu.id == RoleMenu.menu_id)
                & (Menu.is_deleted.is_(False))
                & (Menu.status == "active")
                & (Menu.menu_type == "permission"),
            )
            .where(
                UserRole.user_id == user.id,
                UserRole.is_deleted.is_(False),
                Role.is_deleted.is_(False),
                Role.status == "active",
            )
            .distinct(),
        )
        rows = result.all()

        # admin 角色直接放行
        if any(code and code.upper() == _ADMIN_ROLE_CODE for code, _ in rows):
            return user

        # 任一角色绑定该权限菜单即放行
        if any(key == permission for _, key in rows):
            return user

        raise ForbiddenException(ErrorCode.PERMISSION_DENIED, get_msg("PERMISSION_DENIED"))
    return permission_checker


# ── 缓存服务 ────────────────────────────────────────────────

def get_cache_service() -> CacheService:
    """获取缓存服务单例（根据 settings.cache_type 选择后端）"""
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService(cache_type=settings.cache_type)
    return _cache_service
