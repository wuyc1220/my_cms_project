"""
敏感词检查服务

在中间件层拦截写操作请求，提取请求体中的文本字段，
与敏感词库（status=active 且 is_deleted=False）进行匹配，
命中则阻止提交并返回统一提示。

支持通配符：* 匹配任意字符序列，? 匹配单个字符。
"""

import fnmatch
import re
from datetime import datetime, timedelta
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class SensitiveWordEntry:
    __slots__ = ("keyword", "type_code", "_pattern")

    def __init__(self, keyword: str, type_code: str):
        self.keyword = keyword
        self.type_code = type_code
        self._pattern: Optional[re.Pattern] = None

    def matches(self, text: str) -> bool:
        if self._pattern is None:
            regex = fnmatch.translate(self.keyword)
            if regex.endswith("\\Z"):
                regex = regex[:-2]
            self._pattern = re.compile(regex, re.IGNORECASE)
        return bool(self._pattern.search(text))


class SensitiveCheckService:
    _instance: Optional["SensitiveCheckService"] = None

    def __init__(self):
        self._words_cache: list[SensitiveWordEntry] = []
        self._cache_expires_at: Optional[datetime] = None
        self._cache_ttl: int = 300

    @classmethod
    def get_instance(cls) -> "SensitiveCheckService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def _load_active_words(self, db: AsyncSession) -> list[SensitiveWordEntry]:
        now = datetime.now()
        if self._words_cache and self._cache_expires_at and now < self._cache_expires_at:
            return self._words_cache

        from app.internal.cms_biz_system.models.sensitive_word import SensitiveWord

        result = await db.execute(
            select(SensitiveWord.keyword, SensitiveWord.type_code).where(
                SensitiveWord.status == "active",
                SensitiveWord.is_deleted == False,
            )
        )
        rows = result.all()
        self._words_cache = [SensitiveWordEntry(keyword=r[0], type_code=r[1]) for r in rows]
        self._cache_expires_at = now + timedelta(seconds=self._cache_ttl)
        logger.debug("敏感词缓存已刷新 | count={}", len(self._words_cache))
        return self._words_cache

    def invalidate_cache(self) -> None:
        self._words_cache = []
        self._cache_expires_at = None

    async def check_text(self, db: AsyncSession, text: str) -> bool:
        if not text:
            return False
        words = await self._load_active_words(db)
        for word in words:
            if word.matches(text):
                return True
        return False

    async def find_matches(self, db: AsyncSession, text: str) -> list[str]:
        """返回文本命中的所有敏感词关键字（供元数据质量检查等需要定位命中词的场景使用）。"""
        if not text:
            return []
        words = await self._load_active_words(db)
        return [word.keyword for word in words if word.matches(text)]

    async def check_value(self, db: AsyncSession, value) -> bool:
        if isinstance(value, str) and value.strip():
            return await self.check_text(db, value)
        return False

    async def check_dict(self, db: AsyncSession, data: dict) -> bool:
        for value in data.values():
            if await self._check_recursive(db, value):
                return True
        return False

    async def _check_recursive(self, db: AsyncSession, value) -> bool:
        if isinstance(value, str) and value.strip():
            if await self.check_text(db, value):
                return True
        elif isinstance(value, dict):
            for v in value.values():
                if await self._check_recursive(db, v):
                    return True
        elif isinstance(value, list):
            for item in value:
                if await self._check_recursive(db, item):
                    return True
        return False
