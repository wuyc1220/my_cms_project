"""
用户数据访问层
封装所有对 User / Role / UserRole 表的 SQLAlchemy 查询操作
"""
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.internal.cms_biz_system.models.user import Role, User, UserRole


# ── User 基础查询 ──────────────────────────────────────────

async def get_user_by_id(db: AsyncSession, user_id: int) -> User | None:
    """按ID查询用户（含角色加载）"""
    result = await db.execute(
        select(User)
        .where(User.id == user_id, User.is_deleted.is_(False))
        .options(selectinload(User.roles).selectinload(UserRole.role))
    )
    return result.scalar_one_or_none()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """按用户名查询用户"""
    result = await db.execute(
        select(User).where(User.username == username, User.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


async def list_users_query(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    username: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    phone_number: str | None = None,
    status: str | None = None,
    role_ids: list[int] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> tuple[list[User], int]:
    """
    分页查询用户列表，返回 (users, total)
    """
    query = select(User).where(User.is_deleted.is_(False))

    # 过滤条件
    if username:
        query = query.where(User.username.ilike(f"%{username}%"))
    if display_name:
        query = query.where(User.display_name.ilike(f"%{display_name}%"))
    if email:
        query = query.where(User.email.ilike(f"%{email}%"))
    if phone_number:
        query = query.where(User.phone_number.ilike(f"%{phone_number}%"))
    if status:
        query = query.where(User.status == status)
    if role_ids:
        query = query.join(UserRole, UserRole.user_id == User.id).where(UserRole.role_id.in_(role_ids)).distinct()

    # 计数
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    # 排序
    if sort_by and sort_order:
        sort_column = getattr(User, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(User.id.desc())
    else:
        query = query.order_by(User.id.desc())

    # 分页
    result = await db.execute(
        query.options(selectinload(User.roles).selectinload(UserRole.role))
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    users = result.scalars().all()

    return users, total


async def get_users_by_ids(db: AsyncSession, ids: list[int]) -> list[User]:
    """按ID列表查询用户"""
    result = await db.execute(
        select(User).where(User.id.in_(ids), User.is_deleted.is_(False))
    )
    return result.scalars().all()


# ── User 写入操作 ──────────────────────────────────────────

async def add_user(db: AsyncSession, user: User) -> None:
    """新增用户"""
    db.add(user)
    await db.flush()


async def delete_user_roles(db: AsyncSession, user_id: int) -> None:
    """删除用户的所有角色关联"""
    await db.execute(delete(UserRole).where(UserRole.user_id == user_id))
    await db.flush()


async def add_user_role(db: AsyncSession, user_id: int, role_id: int) -> None:
    """新增用户角色关联"""
    db.add(UserRole(user_id=user_id, role_id=role_id))


# ── Role 查询 ──────────────────────────────────────────────

async def get_roles_by_ids(db: AsyncSession, role_ids: list[int]) -> list[Role]:
    """按ID列表查询角色"""
    if not role_ids:
        return []
    result = await db.execute(
        select(Role).where(Role.id.in_(role_ids), Role.is_deleted.is_(False))
    )
    return result.scalars().all()


async def list_roles_query(
    db: AsyncSession,
    *,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    code: str | None = None,
    status: str | None = None,
) -> tuple[list[Role], int]:
    """分页查询角色列表"""
    query = select(Role).where(Role.is_deleted.is_(False))
    if name:
        query = query.where(Role.name.ilike(f"%{name}%"))
    if code:
        query = query.where(Role.code.ilike(f"%{code}%"))
    if status:
        query = query.where(Role.status == status)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(
        query.order_by(Role.id.desc()).offset((page - 1) * page_size).limit(page_size)
    )
    return result.scalars().all(), total


async def get_all_roles(db: AsyncSession) -> list[Role]:
    """获取所有角色（不分页）"""
    result = await db.execute(
        select(Role).where(Role.is_deleted.is_(False)).order_by(Role.id)
    )
    return result.scalars().all()


async def get_role_by_id(db: AsyncSession, role_id: int) -> Role | None:
    """按ID查询角色"""
    result = await db.execute(
        select(Role).where(Role.id == role_id, Role.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


async def get_role_by_code(db: AsyncSession, code: str) -> Role | None:
    """按code查询角色"""
    result = await db.execute(
        select(Role).where(Role.code == code, Role.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


async def add_role(db: AsyncSession, role: Role) -> None:
    """新增角色"""
    db.add(role)
    await db.flush()
