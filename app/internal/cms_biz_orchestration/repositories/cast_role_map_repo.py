"""
CastRoleMap Repository 层。

封装 content_cast_role_map 表的 SQLAlchemy 查询操作。
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.internal.cms_biz_orchestration.models.cast_role_map import CastRoleMap


async def get_cast_role_map_by_id(db: AsyncSession, map_id: int) -> CastRoleMap | None:
    """按 ID 查询 CastRoleMap。"""
    return (
        await db.execute(
            select(CastRoleMap)
            .where(
                CastRoleMap.map_id == map_id,
                CastRoleMap.is_deleted.is_(False), CastRoleMap.is_discarded.is_(False),
            )
            .options(joinedload(CastRoleMap.cast))
        )
    ).scalar_one_or_none()


async def list_cast_role_maps(
    db: AsyncSession,
    *,
    program_id: int | None = None,
    movie_id: int | None = None,
    cast_id: int | None = None,
    content_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> tuple[list[CastRoleMap], int]:
    """分页查询 CastRoleMap 列表。"""
    query = (
        select(CastRoleMap)
        .where(CastRoleMap.is_deleted.is_(False), CastRoleMap.is_discarded.is_(False))
        .options(joinedload(CastRoleMap.cast))
    )
    count_query = select(func.count(CastRoleMap.map_id)).where(CastRoleMap.is_deleted.is_(False), CastRoleMap.is_discarded.is_(False))

    if content_id is not None:
        query = query.where(CastRoleMap.content_id == content_id)
        count_query = count_query.where(CastRoleMap.content_id == content_id)
    if program_id is not None:
        query = query.where(CastRoleMap.program_id == program_id)
        count_query = count_query.where(CastRoleMap.program_id == program_id)
    if movie_id is not None:
        query = query.where(CastRoleMap.movie_id == movie_id)
        count_query = count_query.where(CastRoleMap.movie_id == movie_id)
    if cast_id is not None:
        query = query.where(CastRoleMap.cast_id == cast_id)
        count_query = count_query.where(CastRoleMap.cast_id == cast_id)

    total = (await db.execute(count_query)).scalar() or 0
    results = (
        await db.execute(
            query.order_by(CastRoleMap.map_id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return list(results), total


async def add_cast_role_map(db: AsyncSession, cast_role_map: CastRoleMap) -> None:
    """新增 CastRoleMap。"""
    db.add(cast_role_map)


async def update_cast_role_map(db: AsyncSession, cast_role_map: CastRoleMap, **kwargs) -> None:
    """更新 CastRoleMap 字段。"""
    for key, value in kwargs.items():
        if hasattr(cast_role_map, key):
            setattr(cast_role_map, key, value)


async def delete_cast_role_map(db: AsyncSession, map_id: int) -> bool:
    """软删除 CastRoleMap。"""
    cast_role_map = await get_cast_role_map_by_id(db, map_id)
    if cast_role_map:
        cast_role_map.is_deleted = True
        return True
    return False
