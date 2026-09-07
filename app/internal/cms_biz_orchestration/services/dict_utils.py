"""
字典字段严格匹配公共函数模块。

提供统一的字典字段严格匹配逻辑，用于 VOD 导入、归档导入、节目单导入。
"""

from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_system.services.dict_service import get_dict_children_by_code


async def resolve_dict_codes_strict(
    db: AsyncSession,
    dict_code: str,
    text_value: str | None,
    dict_caches: dict[str, dict[str, str]],
    errors: list[str],
    field_label: str | None = None,
) -> list[str]:
    """解析字典字段（多值，严格模式）：仅按 name 匹配 code，未匹配不创建，记录错误。

    Args:
        db: 数据库会话
        dict_code: 字典根节点 code（如 "VodType"、"Language"）
        text_value: Excel 中的文本值（逗号分隔，如 "暴力,恐怖"）
        dict_caches: 字典缓存 {dict_code: {name: code}}
        errors: 错误列表，匹配失败时追加错误消息
        field_label: 字段标签（用于错误消息，如 "Advice"）

    Returns:
        匹配成功的 code 列表（如 ["violence", "horror"]），匹配失败时返回空列表
    """
    if not text_value:
        return []
    names = [n.strip() for n in str(text_value).split(",") if n.strip()]
    if not names:
        return []
    cache = dict_caches.setdefault(dict_code, {})
    if not cache:
        # 缓存未初始化，查询数据库并缓存 {name: code} 映射
        dict_items = await get_dict_children_by_code(db, dict_code)
        for item in dict_items:
            cache[item.name] = item.code
    label = field_label or dict_code
    codes: list[str] = []
    all_matched = True
    for name in names:
        code = cache.get(name)
        if code:
            codes.append(code)
        else:
            errors.append(f"{label} not found: {name}")
            all_matched = False
    return codes if all_matched else []


async def resolve_single_dict_code_strict(
    db: AsyncSession,
    dict_code: str,
    text_value: str | None,
    dict_caches: dict[str, dict[str, str]],
    errors: list[str],
    field_label: str | None = None,
) -> Optional[str]:
    """解析字典字段（单值，严格模式）：仅按 name 匹配 code，未匹配不创建，记录错误。

    Args:
        db: 数据库会话
        dict_code: 字典根节点 code（如 "VodType"、"RatingLevel"）
        text_value: Excel 中的文本值（如 "电影"）
        dict_caches: 字典缓存 {dict_code: {name: code}}
        errors: 错误列表，匹配失败时追加错误消息
        field_label: 字段标签（用于错误消息，如 "VodType"）

    Returns:
        匹配成功的 code（如 "movie"），匹配失败时返回 None
    """
    if not text_value:
        return None
    name = str(text_value).strip()
    if not name:
        return None
    cache = dict_caches.setdefault(dict_code, {})
    if not cache:
        # 缓存未初始化，查询数据库并缓存 {name: code} 映射
        dict_items = await get_dict_children_by_code(db, dict_code)
        for item in dict_items:
            cache[item.name] = item.code
    code = cache.get(name)
    if code:
        return code
    label = field_label or dict_code
    errors.append(f"{label} not found: {name}")
    return None