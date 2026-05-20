import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_system.models.user import Role
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.schemas.user_crud import RoleCreate, RoleListItem, RoleUpdate
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


async def _get_role_or_404(db: AsyncSession, role_id: int) -> Role:
    """获取角色，不存在则404"""
    role = (await db.execute(
        select(Role).where(Role.id == role_id, Role.is_deleted.is_(False))
    )).scalar_one_or_none()
    if not role:
        raise NotFoundException(ErrorCode.ROLE_NOT_FOUND, get_msg("ROLE_NOT_FOUND"))
    return role


async def get_role_by_id(db: AsyncSession, role_id: int) -> Role:
    """按ID获取角色（含404处理）"""
    return await _get_role_or_404(db, role_id)


async def list_roles(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    code: str | None = None,
    name: str | None = None,
    description: str | None = None,
    status: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[RoleListItem]:
    query = select(Role).where(Role.is_deleted.is_(False))
    if code:
        query = query.where(Role.code.ilike(f"%{code}%"))
    if name:
        query = query.where(Role.name.ilike(f"%{name}%"))
    if description:
        query = query.where(Role.description.ilike(f"%{description}%"))
    if status:
        query = query.where(Role.status == status)

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Role, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func)
        else:
            query = query.order_by(Role.id.desc())
    else:
        query = query.order_by(Role.id.desc())

    roles = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[RoleListItem.model_validate(r) for r in roles],
    )


async def get_all_roles(db: AsyncSession) -> list[RoleListItem]:
    roles = (
        await db.execute(
            select(Role).where(Role.status == "active", Role.is_deleted.is_(False)).order_by(Role.name)
        )
    ).scalars().all()
    return [RoleListItem.model_validate(r) for r in roles]


async def create_role(db: AsyncSession, data: RoleCreate) -> Role:
    code = (data.code or "").strip() or uuid.uuid4().hex[:8].upper()
    # Ensure unique code (排除已删除数据)
    existing = (await db.execute(
        select(Role.id).where(Role.code == code, Role.is_deleted.is_(False)).limit(1)
    )).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.ROLE_CODE_EXISTS, get_msg("ROLE_CODE_EXISTS", code=code))
    existing_name = (await db.execute(
        select(Role.id).where(Role.name == data.name, Role.is_deleted.is_(False)).limit(1)
    )).scalar_one_or_none()
    if existing_name:
        raise BusinessException(ErrorCode.ROLE_NAME_EXISTS, get_msg("ROLE_NAME_EXISTS"))
    role = Role(
        name=data.name,
        code=code,
        status=data.status,
        description=data.description,
        is_system=False,
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


async def update_role(db: AsyncSession, role_id: int, data: RoleUpdate) -> Role:
    role = await _get_role_or_404(db, role_id)
    if data.name is not None:
        existing_name = (await db.execute(
            select(Role.id).where(Role.name == data.name, Role.id != role_id, Role.is_deleted.is_(False)).limit(1)
        )).scalar_one_or_none()
        if existing_name:
            raise BusinessException(ErrorCode.ROLE_NAME_EXISTS, get_msg("ROLE_NAME_EXISTS"))
        role.name = data.name
    if data.status is not None:
        role.status = data.status
    if data.description is not None:
        role.description = data.description
    await db.commit()
    await db.refresh(role)
    return role


async def delete_role(db: AsyncSession, role_id: int) -> None:
    role = await _get_role_or_404(db, role_id)
    if role.is_system:
        raise BusinessException(ErrorCode.SYSTEM_ROLE_CANNOT_DELETE, get_msg("SYSTEM_ROLE_CANNOT_DELETE"))
    role.is_deleted = True
    await db.commit()


async def toggle_role_status(db: AsyncSession, role_id: int, new_status: str) -> Role:
    role = await _get_role_or_404(db, role_id)
    role.status = new_status
    await db.commit()
    await db.refresh(role)
    return role


async def batch_update_role_status(db: AsyncSession, ids: list[int], new_status: str) -> int:
    roles = (await db.execute(
        select(Role).where(Role.id.in_(ids), Role.is_deleted.is_(False))
    )).scalars().all()
    for r in roles:
        if new_status == "deleted":
            r.is_deleted = True
        else:
            r.status = new_status
    await db.commit()
    return len(roles)


async def batch_delete_roles(db: AsyncSession, ids: list[int]) -> int:
    roles = (await db.execute(
        select(Role).where(Role.id.in_(ids), Role.is_deleted.is_(False))
    )).scalars().all()
    for r in roles:
        if r.is_system:
            raise BusinessException(ErrorCode.SYSTEM_ROLE_CANNOT_DELETE, get_msg("SYSTEM_ROLE_CANNOT_DELETE"))
    for r in roles:
        r.is_deleted = True
    await db.commit()
    return len(roles)
