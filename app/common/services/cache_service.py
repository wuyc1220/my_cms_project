"""
通用缓存服务

设计思路：
- CacheBackend 为抽象基类，定义 set/get/delete/clean_expired 接口
- DatabaseCacheBackend 基于 cache_store 表实现（默认）
- 未来可扩展 RedisCacheBackend，通过 .env 的 cache_type 切换
- CacheService 为门面类，根据配置自动选择后端

使用方式：
    from app.common.services.cache_service import cache_service

    # 写入缓存（TTL 5分钟）
    await cache_service.set(db, "captcha:xxx", {"code": "abc"}, ttl_seconds=300)

    # 读取缓存
    data = await cache_service.get(db, "captcha:xxx")

    # 删除缓存
    await cache_service.delete(db, "captcha:xxx")
"""

from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.models.cache_store import CacheStore


class CacheBackend(ABC):
    """缓存后端抽象基类，所有实现必须继承此类"""

    @abstractmethod
    async def set(self, db: AsyncSession, key: str, value: dict, ttl_seconds: int) -> None:
        """写入缓存，ttl_seconds 为存活秒数"""

    @abstractmethod
    async def get(self, db: AsyncSession, key: str) -> dict | None:
        """读取缓存，过期或不存在返回 None"""

    @abstractmethod
    async def delete(self, db: AsyncSession, key: str) -> None:
        """删除缓存"""

    @abstractmethod
    async def clean_expired(self, db: AsyncSession) -> int:
        """清理所有过期缓存，返回清理数量"""


class DatabaseCacheBackend(CacheBackend):
    """基于 PostgreSQL cache_store 表的缓存实现"""

    async def set(self, db: AsyncSession, key: str, value: dict, ttl_seconds: int) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        existing = (await db.execute(
            select(CacheStore).where(CacheStore.key == key)
        )).scalar_one_or_none()

        if existing:
            existing.value = value
            existing.expires_at = expires_at
        else:
            db.add(CacheStore(key=key, value=value, expires_at=expires_at))
        await db.flush()

    async def get(self, db: AsyncSession, key: str) -> dict | None:
        row = (await db.execute(
            select(CacheStore).where(CacheStore.key == key)
        )).scalar_one_or_none()

        if not row:
            return None

        if datetime.now(timezone.utc) > row.expires_at:
            await db.execute(delete(CacheStore).where(CacheStore.key == key))
            await db.flush()
            return None

        return row.value

    async def delete(self, db: AsyncSession, key: str) -> None:
        await db.execute(delete(CacheStore).where(CacheStore.key == key))
        await db.flush()

    async def clean_expired(self, db: AsyncSession) -> int:
        now = datetime.now(timezone.utc)
        result = await db.execute(
            delete(CacheStore).where(CacheStore.expires_at < now)
        )
        await db.flush()
        count = result.rowcount
        if count > 0:
            logger.info("缓存清理完成 | deleted={}", count)
        return count


class CacheService:
    """
    缓存服务门面

    通过 cache_type 配置切换后端实现：
    - "database"（默认）: 使用 cache_store 表
    - "redis": 预留，未来实现 RedisCacheBackend 后启用
    """

    def __init__(self, cache_type: str = "database"):
        self._backend: CacheBackend
        if cache_type == "redis":
            raise NotImplementedError("Redis 缓存后端尚未实现，请使用 cache_type=database")
        self._backend = DatabaseCacheBackend()
        logger.info("缓存服务初始化 | backend=database")

    async def set(self, db: AsyncSession, key: str, value: dict, ttl_seconds: int) -> None:
        await self._backend.set(db, key, value, ttl_seconds)

    async def get(self, db: AsyncSession, key: str) -> dict | None:
        return await self._backend.get(db, key)

    async def delete(self, db: AsyncSession, key: str) -> None:
        await self._backend.delete(db, key)

    async def clean_expired(self, db: AsyncSession) -> int:
        return await self._backend.clean_expired(db)
