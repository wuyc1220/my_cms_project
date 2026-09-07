"""数据权限管理 - 业务逻辑层

核心功能：
1. 查询内容列表并关联授权信息
2. 授权（覆盖式：先删除旧授权，再新增）
3. 清除授权（物理删除）
4. 获取角色/用户下拉选项
"""

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.internal.cms_biz_system.models.content_auth import ContentAuth
from app.internal.cms_biz_system.models.user import Role, User, UserRole
from app.internal.cms_biz_package.models.package import Content
from app.internal.cms_biz_system.schemas.content_auth import (
    AuthorizedRoleItem,
    AuthorizedUserItem,
    ContentAuthListItem,
    RoleSimpleItem,
    UserSimpleItem,
)
from app.common.schemas import PaginatedResponse


# ─── 查询内容列表（带授权信息）────────────────────────────────────

async def list_auth_contents(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    content_name: str | None = None,
    content_types: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
    authorized_user: str | None = None,
    authorized_role: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[ContentAuthListItem]:
    """查询内容列表并关联授权信息"""

    # 基础查询：未被删除的内容
    query = select(Content).where(Content.is_deleted.is_(False), Content.is_discarded.is_(False))

    # 过滤条件
    if content_name:
        query = query.where(Content.title.ilike(f"%{content_name}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if ingest_statuses:
        query = query.where(Content.status.in_(ingest_statuses))

    # 按授权角色/用户过滤（子查询）
    if authorized_role:
        role_subquery = (
            select(ContentAuth.content_id)
            .join(Role, Role.id == ContentAuth.role_id)
            .where(
                Role.name.ilike(f"%{authorized_role}%"),
                ContentAuth.is_deleted == False,
            )
        )
        query = query.where(Content.id.in_(role_subquery))

    if authorized_user:
        user_subquery = (
            select(ContentAuth.content_id)
            .join(User, User.id == ContentAuth.user_id)
            .where(
                (User.display_name.ilike(f"%{authorized_user}%")) |
                (User.username.ilike(f"%{authorized_user}%")),
                ContentAuth.is_deleted == False,
            )
        )
        query = query.where(Content.id.in_(user_subquery))

    # 总数
    count_query = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_query)).scalar_one()

    # 动态排序（默认按 ID 倒序）
    # 前端字段名 -> 数据库字段名映射
    sort_field_map = {
        'content_name': 'title',
        'ingest_status': 'status',
        'content_type': 'content_type',
    }
    if sort_by and sort_order:
        db_field = sort_field_map.get(sort_by, sort_by)
        sort_column = getattr(Content, db_field, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(Content.id.desc())
    else:
        query = query.order_by(Content.id.desc())

    # 分页
    query = query.offset((page - 1) * page_size).limit(page_size)
    contents = (await db.execute(query)).scalars().all()

    # 批量查询授权信息
    content_ids = [c.id for c in contents]
    auth_map: dict[int, dict] = {cid: {"roles": [], "users": []} for cid in content_ids}

    if content_ids:
        # 查有效授权的角色
        role_auths = (
            await db.execute(
                select(ContentAuth.content_id, Role.id, Role.name)
                .join(Role, Role.id == ContentAuth.role_id)
                .where(
                    ContentAuth.content_id.in_(content_ids),
                    ContentAuth.role_id.isnot(None),
                    ContentAuth.is_deleted == False,
                )
            )
        ).all()
        for row in role_auths:
            auth_map[row[0]]["roles"].append(AuthorizedRoleItem(id=row[1], name=row[2]))

        # 查有效授权的用户
        user_auths = (
            await db.execute(
                select(ContentAuth.content_id, User.id, User.display_name, User.username)
                .join(User, User.id == ContentAuth.user_id)
                .where(
                    ContentAuth.content_id.in_(content_ids),
                    ContentAuth.user_id.isnot(None),
                    ContentAuth.is_deleted == False,
                )
            )
        ).all()
        for row in user_auths:
            auth_map[row[0]]["users"].append(
                AuthorizedUserItem(id=row[1], display_name=row[2], username=row[3])
            )

    # 组装结果
    items = [
        ContentAuthListItem(
            id=c.id,
            content_name=c.title,
            content_type=c.content_type,
            ingest_status=c.status,
            authorized_roles=auth_map[c.id]["roles"],
            authorized_users=auth_map[c.id]["users"],
        )
        for c in contents
    ]

    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)


# ─── 授权（覆盖式）───────────────────────────────────────────────

async def authorize_contents(
    db: AsyncSession,
    content_ids: list[int],
    role_ids: list[int] | None,
    user_ids: list[int] | None,
    created_by: int | None = None,
) -> None:
    """覆盖式授权：先物理删除旧授权，再新增

    role_ids / user_ids 为 None 或空列表均表示不绑定对应维度。
    """
    # None → 空列表，统一处理
    _role_ids = role_ids or []
    _user_ids = user_ids or []

    # 1. 物理删除这些内容的所有现有授权（覆盖式，旧数据无保留价值，操作日志已记录）
    await db.execute(
        delete(ContentAuth).where(ContentAuth.content_id.in_(content_ids))
    )

    # 2. 新增角色授权
    for content_id in content_ids:
        for role_id in _role_ids:
            db.add(ContentAuth(
                content_id=content_id,
                role_id=role_id,
                user_id=None,
                is_deleted=False,
                created_by=created_by,
            ))

    # 3. 新增用户授权
    for content_id in content_ids:
        for user_id in _user_ids:
            db.add(ContentAuth(
                content_id=content_id,
                role_id=None,
                user_id=user_id,
                is_deleted=False,
                created_by=created_by,
            ))

    await db.flush()
    logger.info(f"authorize_contents: content_ids={content_ids}, role_ids={_role_ids}, user_ids={_user_ids}")


# ─── 清除授权 ────────────────────────────────────────────────────

async def clear_auth(
    db: AsyncSession,
    content_ids: list[int],
) -> None:
    """物理删除指定内容的全部授权"""
    await db.execute(
        delete(ContentAuth).where(ContentAuth.content_id.in_(content_ids))
    )
    await db.flush()
    logger.info(f"clear_auth: content_ids={content_ids}")


async def get_auth_snapshot_map(
    db: AsyncSession,
    content_ids: list[int],
) -> dict[int, dict[str, list[int]]]:
    """批量查询指定 content_ids 的授权快照，返回 {content_id: {role_ids: [...], user_ids: [...]}}"""
    snapshot: dict[int, dict[str, list[int]]] = {cid: {"role_ids": [], "user_ids": []} for cid in content_ids}
    if not content_ids:
        return snapshot
    role_auths = (
        await db.execute(
            select(ContentAuth.content_id, ContentAuth.role_id)
            .where(
                ContentAuth.content_id.in_(content_ids),
                ContentAuth.role_id.isnot(None),
                ContentAuth.is_deleted == False,
            )
        )
    ).all()
    for row in role_auths:
        if row[1] is not None:
            snapshot[row[0]]["role_ids"].append(row[1])
    user_auths = (
        await db.execute(
            select(ContentAuth.content_id, ContentAuth.user_id)
            .where(
                ContentAuth.content_id.in_(content_ids),
                ContentAuth.user_id.isnot(None),
                ContentAuth.is_deleted == False,
            )
        )
    ).all()
    for row in user_auths:
        if row[1] is not None:
            snapshot[row[0]]["user_ids"].append(row[1])
    return snapshot


# ─── 角色下拉选项 ────────────────────────────────────────────────

async def get_roles_for_select(db: AsyncSession) -> list[RoleSimpleItem]:
    result = await db.execute(
        select(Role).where(Role.status == "active", Role.is_deleted == False).order_by(Role.id)
    )
    roles = result.scalars().all()
    return [RoleSimpleItem(id=r.id, name=r.name, code=r.code) for r in roles]


# ─── 用户下拉选项 ────────────────────────────────────────────────

async def get_users_for_select(db: AsyncSession) -> list[UserSimpleItem]:
    result = await db.execute(
        select(User).where(User.status == "active", User.is_deleted == False).order_by(User.id)
    )
    users = result.scalars().all()
    return [UserSimpleItem(id=u.id, display_name=u.display_name, username=u.username) for u in users]


# ─── 检查用户是否有内容的数据权限 ──────────────────────────────

async def check_content_auth_permission(
    db: AsyncSession,
    user_id: int,
    content_id: int,
) -> bool:
    """检查指定用户是否有访问指定内容的权限。
    
    权限规则（满足任一即可）：
    1. 用户是该内容的创建者（Content.created_by == user_id）
    2. content_auth 表中存在授权记录（user_id 或 role_id 匹配）
    
    Returns:
        True: 有权限
        False: 无权限
    """
    from app.internal.cms_biz_system.services.data_auth_filter import is_admin_user
    from app.internal.cms_biz_system.models.user import User as UserModel
    
    # 1. 检查用户是否存在
    user = await db.get(UserModel, user_id)
    if not user:
        return False
    
    # 2. ADMIN 角色直接放行
    if await is_admin_user(db, user):
        return True
    
    # 3. 检查是否是创建者
    content = await db.get(Content, content_id)
    if not content:
        return False
    
    if content.created_by == user_id:
        return True
    
    # 4. 检查 content_auth 表中是否有授权
    # 4.1 用户维度授权
    user_auth = (
        await db.execute(
            select(ContentAuth.id)
            .where(
                ContentAuth.content_id == content_id,
                ContentAuth.user_id == user_id,
                ContentAuth.is_deleted == False,
            )
        )
    ).scalar_one_or_none()
    
    if user_auth:
        return True
    
    # 4.2 角色维度授权
    from sqlalchemy import or_
    
    role_ids_result = await db.execute(
        select(UserRole.role_id)
        .where(
            UserRole.user_id == user_id,
            UserRole.is_deleted == False,
        )
    )
    role_ids = [row[0] for row in role_ids_result.all() if row[0] is not None]
    
    if role_ids:
        role_auth = (
            await db.execute(
                select(ContentAuth.id)
                .where(
                    ContentAuth.content_id == content_id,
                    ContentAuth.role_id.in_(role_ids),
                    ContentAuth.is_deleted == False,
                )
            )
        ).scalar_one_or_none()
        
        if role_auth:
            return True
    
    return False
