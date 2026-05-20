"""内容数据权限 - 通用过滤工具

用于在"交易管理下的内容管理"等涉及 Content 的列表查询场景中，
统一接入数据权限过滤逻辑。

过滤规则：
1. 预置角色 admin（忽略大小写）账号不受控制，可看到全部内容数据；
2. 其他角色账号只能看到以下两类数据的并集：
   - 规则 A：谁创建的谁能看（Content.created_by == current_user.id）；
   - 规则 B：在 content_auth 表中被授权给当前用户或当前用户所属任一角色的内容
            （user_id = current_user.id  OR  role_id IN 当前用户角色ID集合）。

对外主要提供三个函数：
- is_admin_user(db, user)                  判定是否为 admin
- build_content_data_auth_condition(...)   构造可直接用于 .where() 的过滤条件
                                           （admin 返回 None 表示不过滤）
- apply_content_data_auth(db, user, query) 直接把过滤条件拼到给定 Select 上

使用示例：
    from app.internal.cms_biz_system.services.data_auth_filter import (
        apply_content_data_auth,
    )

    query = select(Content).where(Content.is_deleted.is_(False))
    query = await apply_content_data_auth(db, current_user, query)
    result = await db.execute(query)
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select
from sqlalchemy.sql.elements import ColumnElement

from app.internal.cms_biz_system.models.content_auth import ContentAuth
from app.internal.cms_biz_system.models.user import Role, User, UserRole
from app.internal.cms_biz_package.models.package import Content


# 预置 admin 角色 code（比较时统一大写）
ADMIN_ROLE_CODE = "ADMIN"

TASK_RELATED_MODULE_CODES = frozenset({
    "task_completion_stats",
    "task_status_count",
    "task_assigned_table",
    "not_assigned_tasks",
})


# ─── 判定是否 admin ────────────────────────────────────────────────

async def is_admin_user(db: AsyncSession, user: Optional[User]) -> bool:
    """判断用户是否拥有预置 admin 角色（忽略大小写）。

    规则：
    - user 为 None 时，视为非 admin；
    - 仅统计未删除且 status='active' 的角色；
    - 只要存在一个角色 code 大写等于 "ADMIN" 即视为 admin。
    """
    if user is None or getattr(user, "id", None) is None:
        return False

    result = await db.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user.id,
            UserRole.is_deleted.is_(False),
            Role.is_deleted.is_(False),
            Role.status == "active",
        )
    )
    for (code,) in result.all():
        if code and code.upper() == ADMIN_ROLE_CODE:
            return True
    return False


async def has_task_assign_role(db: AsyncSession, user: Optional[User]) -> bool:
    """判断用户是否拥有 TASK_ASSIGN 角色（ADMIN 视为拥有）。

    用于看板模块显隐控制：只有拥有 TASK_ASSIGN 角色的用户
    才能看到任务相关的四个模块。
    """
    if await is_admin_user(db, user):
        return True
    if user is None or getattr(user, "id", None) is None:
        return False
    result = await db.execute(
        select(Role.code)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(
            UserRole.user_id == user.id,
            UserRole.is_deleted.is_(False),
            Role.is_deleted.is_(False),
            Role.status == "active",
        )
    )
    return any(code and code.upper() == "TASK_ASSIGN" for (code,) in result.all())


# ─── 获取当前用户角色 ID 集合 ──────────────────────────────────────

async def _get_user_role_ids(db: AsyncSession, user_id: int) -> list[int]:
    """获取用户当前绑定的角色 ID 列表（仅有效关联 + 有效角色）。"""
    result = await db.execute(
        select(UserRole.role_id)
        .join(Role, Role.id == UserRole.role_id)
        .where(
            UserRole.user_id == user_id,
            UserRole.is_deleted.is_(False),
            Role.is_deleted.is_(False),
            Role.status == "active",
        )
    )
    return [row[0] for row in result.all() if row[0] is not None]


# ─── 构造 Content 数据权限过滤条件 ────────────────────────────────

async def build_content_data_auth_condition(
    db: AsyncSession,
    current_user: Optional[User],
) -> Optional[ColumnElement[bool]]:
    """构造针对 Content 表的数据权限过滤条件。

    返回值：
    - admin 用户 → 返回 None（调用方拿到 None 表示无需过滤）；
    - 其他用户  → 返回一个可直接用于 query.where(...) 的布尔条件，
                  等价于:
                  (Content.created_by = :uid)
                  OR Content.id IN (
                      SELECT content_id FROM content_auth
                       WHERE is_deleted = false
                         AND ( user_id = :uid OR role_id IN (:role_ids) )
                  )

    说明：
    - 当用户既不是 admin、又无任何角色、且 content_auth 中无用户维度授权时，
      最终只会命中"自己创建的数据"。
    """
    # admin 放行
    if await is_admin_user(db, current_user):
        return None

    # 非登录态的兜底（理论上不会发生，调用方应已保证 current_user 存在）
    if current_user is None or getattr(current_user, "id", None) is None:
        # 不放行任何数据
        return Content.id.is_(None)

    user_id = current_user.id
    role_ids = await _get_user_role_ids(db, user_id)

    # 组装 content_auth 子查询条件：user 维度 + role 维度（任一命中）
    auth_or_conditions: list[ColumnElement[bool]] = [ContentAuth.user_id == user_id]
    if role_ids:
        auth_or_conditions.append(ContentAuth.role_id.in_(role_ids))

    auth_subquery = (
        select(ContentAuth.content_id)
        .where(
            ContentAuth.is_deleted.is_(False),
            or_(*auth_or_conditions),
        )
    )

    # 最终条件：创建人匹配 OR 命中数据权限授权
    return or_(
        Content.created_by == user_id,
        Content.id.in_(auth_subquery),
    )


# ─── 直接把过滤条件拼到 query 上 ───────────────────────────────────

async def apply_content_data_auth(
    db: AsyncSession,
    current_user: Optional[User],
    query: Select,
) -> Select:
    """将内容数据权限过滤条件拼到 query 上并返回新 query。

    - admin  → 原样返回（不加过滤）；
    - 其他   → 在 query 上追加 where 条件。

    调用方只需保证传入的 query 基于 Content 表即可（含 JOIN 的场景亦可用，
    本方法不会破坏已有 where/order_by/limit 等子句）。
    """
    condition = await build_content_data_auth_condition(db, current_user)
    if condition is None:
        return query
    return query.where(condition)
