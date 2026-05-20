"""公共工具类模块"""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

_DIFF_EXCLUDE_KEYS: set[str] = {"updated_at", "updated_by"}


async def is_admin_user(db: AsyncSession, username: str) -> bool:
    """
    检查用户是否为 ADMIN 角色。

    ADMIN 角色用户可以跳过任务指派人权限校验。

    输入：
        db: 数据库会话
        username: 用户名

    输出：
        bool: 是否为 ADMIN 角色
    """
    from app.internal.cms_biz_system.models.user import Role, User, UserRole

    result = await db.execute(
        select(Role.code)
        .join(UserRole, Role.id == UserRole.role_id)
        .join(User, User.id == UserRole.user_id)
        .where(User.username == username, User.is_deleted.is_(False))
    )
    role_codes = list(result.scalars().all())
    return "ADMIN" in [c.upper() for c in role_codes]


def compute_diff_json(
    old_json: str | None,
    new_json: str | None,
    *,
    exclude_keys: set[str] | None = None,
) -> tuple[str | None, str | None]:
    """
    计算两个 JSON 字符串之间的差异，仅返回发生变化的字段。

    用于 Processed History 的 Previous Value / Updated Value 列展示，
    只记录实际变更的字段而非完整实体快照。

    exclude_keys 中的字段不参与差异比较（默认排除 updated_at / updated_by）。
    """
    skip = exclude_keys if exclude_keys is not None else _DIFF_EXCLUDE_KEYS

    old_dict: dict[str, Any] = json.loads(old_json) if old_json else {}
    new_dict: dict[str, Any] = json.loads(new_json) if new_json else {}

    changed_keys: list[str] = []
    all_keys = set(old_dict.keys()) | set(new_dict.keys())

    for key in sorted(all_keys):
        if key in skip:
            continue
        if old_dict.get(key) != new_dict.get(key):
            changed_keys.append(key)

    if not changed_keys:
        return None, None

    prev_diff = {k: old_dict.get(k) for k in changed_keys}
    new_diff = {k: new_dict.get(k) for k in changed_keys}

    return (
        json.dumps(prev_diff, ensure_ascii=False),
        json.dumps(new_diff, ensure_ascii=False),
    )
