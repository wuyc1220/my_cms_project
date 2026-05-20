"""
公共依赖
提供 get_db / get_current_user / _get_ip 等全局依赖注入
"""
from typing import AsyncGenerator

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.common.core.i18n import get_msg
from app.common.core.exceptions import ErrorCode, UnauthorizedException
from app.database import AsyncSessionLocal, set_current_user_id
from app.internal.cms_biz_system.models.user import User

security = HTTPBearer(auto_error=False)


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
    """解析JWT令牌，返回当前用户"""
    if not credentials:
        raise UnauthorizedException(ErrorCode.INVALID_TOKEN, get_msg("INVALID_TOKEN"))
    
    try:
        payload = jwt.decode(credentials.credentials, settings.secret_key, algorithms=["HS256"])
        username: str = payload.get("sub")
        if not username:
            raise UnauthorizedException(ErrorCode.INVALID_TOKEN, get_msg("INVALID_TOKEN"))
    except JWTError:
        raise UnauthorizedException(ErrorCode.INVALID_TOKEN, get_msg("INVALID_TOKEN"))

    result = await db.execute(
        select(User).where(User.username == username, User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    if not user:
        raise UnauthorizedException(ErrorCode.USER_NOT_FOUND, get_msg("USER_NOT_FOUND"))
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


# ── 权限检查（待完善） ──────────────────────────────────────

def require_permission(permission: str):
    """权限检查依赖"""
    def permission_checker(user: User = Depends(get_current_user)):
        # TODO: 实现权限检查
        return user
    return permission_checker
