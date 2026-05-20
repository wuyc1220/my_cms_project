from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import Cast, Picture
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import CastCreate, CastListItem, CastUpdate
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode
from app.internal.cms_biz_orchestration.services.storage import storage_service

async def list_casts(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    cast_id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    ingest_statuses: list[str] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[CastListItem]:
    query = select(Cast).where(Cast.is_deleted.is_(False))
    if cast_id is not None:
        query = query.where(Cast.id == cast_id)
    if name:
        query = query.where(Cast.name.ilike(f"%{name}%"))
    if description:
        query = query.where(Cast.description.ilike(f"%{description}%"))
    if ingest_statuses:
        query = query.where(Cast.ingest_status.in_(ingest_statuses))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Cast, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Cast.id.desc())
        else:
            query = query.order_by(Cast.id.desc())
    else:
        query = query.order_by(Cast.id.desc())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    casts = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    items = []
    for cast in casts:
        poster_url = None
        pic = (
            await db.execute(
                select(Picture.file_path)
                .where(
                    Picture.entity_type.in_(["cast", "Cast"]),
                    Picture.entity_id == cast.id,
                    Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
                )
                .order_by(Picture.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if pic:
            poster_url = storage_service.get_file_url(pic)
        items.append(CastListItem(
            id=cast.id,
            name=cast.name,
            description=cast.description,
            ingest_status=cast.ingest_status,
            status=cast.status,
            poster_url=poster_url,
            created_at=cast.created_at,
        ))

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )

async def get_cast(db: AsyncSession, cast_id: int) -> Cast:
    cast = (
        await db.execute(select(Cast).where(Cast.id == cast_id, Cast.is_deleted.is_(False)))
    ).scalar_one_or_none()
    if not cast:
        raise NotFoundException(ErrorCode.CAST_NOT_FOUND, get_msg("CAST_NOT_FOUND"))
    return cast

async def _check_name_unique(db: AsyncSession, name: str, exclude_id: int | None = None) -> None:
    """校验人物名称唯一性。"""
    query = select(Cast.id).where(Cast.name == name, Cast.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(Cast.id != exclude_id)
    existing = (await db.execute(query.limit(1))).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.CAST_NAME_EXISTS, get_msg("CAST_NAME_EXISTS", name=name))


async def create_cast(db: AsyncSession, data: CastCreate) -> Cast:
    await _check_name_unique(db, data.name)
    cast = Cast(
        name=data.name,
        description=data.description,
        status=data.status,
    )
    db.add(cast)
    await db.commit()
    await db.refresh(cast)
    return cast

async def update_cast(db: AsyncSession, cast_id: int, data: CastUpdate) -> Cast:
    cast = await get_cast(db, cast_id)
    if data.name is not None:
        await _check_name_unique(db, data.name, cast_id)
        cast.name = data.name
    if data.description is not None:
        cast.description = data.description
    if data.ingest_status is not None:
        cast.ingest_status = data.ingest_status
    if data.status is not None:
        cast.status = data.status
    await db.commit()
    await db.refresh(cast)
    return cast

async def delete_cast(db: AsyncSession, cast_id: int) -> None:
    cast = await get_cast(db, cast_id)
    cast.is_deleted = True
    await db.commit()

async def batch_delete_casts(db: AsyncSession, req: BatchDeleteRequest) -> int:
    casts = (
        await db.execute(select(Cast).where(Cast.id.in_(req.ids), Cast.is_deleted.is_(False)))
    ).scalars().all()
    for cast in casts:
        cast.is_deleted = True
    await db.commit()
    return len(casts)
