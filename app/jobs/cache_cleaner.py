"""
缓存过期清理后台任务

独立于 scheduler 基础设施，通过 asyncio.create_task 在应用启动时运行，
每 10 分钟自动清理 cache_store 表中的过期记录。
"""

import asyncio

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.services.cache_service import CacheService
from app.database import AsyncSessionLocal

_CLEAN_INTERVAL_SECONDS = 600


async def run_cache_cleaner() -> None:
    while True:
        try:
            await asyncio.sleep(_CLEAN_INTERVAL_SECONDS)
            cache = CacheService("database")
            async with AsyncSessionLocal() as db:
                count = await cache.clean_expired(db)
                await db.commit()
                if count > 0:
                    logger.info("缓存定时清理完成 | deleted={}", count)
        except asyncio.CancelledError:
            logger.info("缓存定时清理任务已取消")
            break
        except Exception as e:
            logger.error("缓存定时清理异常: {}", e)
