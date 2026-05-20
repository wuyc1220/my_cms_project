from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import PosterSize, PosterSizeBelonging, PosterSizeExtension
from app.internal.cms_biz_metada.schemas.basic import (
    PosterSizeCreate,
    PosterSizeListItem,
    PosterSizeUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from loguru import logger
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


def _to_list_item(ps: PosterSize) -> PosterSizeListItem:
    return PosterSizeListItem.from_orm(ps)


async def _get_poster_size_or_404(db: AsyncSession, poster_size_id: int) -> PosterSize:
    """获取海报尺寸，不存在则404"""
    ps = (await db.execute(
        select(PosterSize).where(PosterSize.id == poster_size_id, PosterSize.is_deleted.is_(False))
    )).scalar_one_or_none()
    if not ps:
        raise NotFoundException(ErrorCode.POSTER_SIZE_NOT_FOUND, get_msg("POSTER_SIZE_NOT_FOUND"))
    return ps


async def list_poster_sizes(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    belongings: list[str] | None = None,
    mandatory: bool | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[PosterSizeListItem]:
    query = select(PosterSize).where(PosterSize.is_deleted.is_(False))
    logger.info(f"list_poster_sizes 入参: page={page}, page_size={page_size}, name={name}, belongings={belongings}, mandatory={mandatory}, sort_by={sort_by}, sort_order={sort_order}")
    if name:
        query = query.where(PosterSize.name.ilike(f"%{name}%"))
    if mandatory is not None:
        query = query.where(PosterSize.mandatory == mandatory)
    if belongings:
        # 过滤含有任一 belonging 的 poster_size
        from sqlalchemy import exists
        for b in belongings:
            query = query.where(
                exists().where(
                    PosterSizeBelonging.poster_size_id == PosterSize.id,
                    PosterSizeBelonging.belonging == b,
                )
            )

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(PosterSize, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, PosterSize.id.desc())
        else:
            query = query.order_by(PosterSize.id.desc())
    else:
        query = query.order_by(PosterSize.id.desc())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    items = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    logger.info(f"list_poster_sizes 出参: total={total}, items_count={len(items)}")
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[_to_list_item(ps) for ps in items],
    )


async def get_poster_size(db: AsyncSession, poster_size_id: int) -> PosterSize:
    logger.info(f"get_poster_size 入参: poster_size_id={poster_size_id}")
    return await _get_poster_size_or_404(db, poster_size_id)


async def create_poster_size(db: AsyncSession, data: PosterSizeCreate) -> PosterSize:
    existing = (await db.execute(
        select(PosterSize.id).where(PosterSize.name == data.name, PosterSize.is_deleted.is_(False)).limit(1)
    )).scalar_one_or_none()
    logger.info(f"create_poster_size 入参: data={data}")
    if existing:
        raise BusinessException(ErrorCode.POSTER_SIZE_NAME_EXISTS, get_msg("POSTER_SIZE_NAME_EXISTS"))
    ps = PosterSize(
        name=data.name,
        width=data.width,
        height=data.height,
        max_file_size_kb=data.max_file_size_kb,
        mapping_type=data.mapping_type,
        mandatory=data.mandatory,
    )
    db.add(ps)
    await db.flush()
    for b in data.belongings:
        db.add(PosterSizeBelonging(poster_size_id=ps.id, belonging=b))
    for e in data.extensions:
        db.add(PosterSizeExtension(poster_size_id=ps.id, extension=e))
    await db.commit()
    await db.refresh(ps)
    return ps


async def update_poster_size(db: AsyncSession, poster_size_id: int, data: PosterSizeUpdate) -> PosterSize:
    ps = await _get_poster_size_or_404(db, poster_size_id)
    logger.info(f"update_poster_size 入参: poster_size_id={poster_size_id}, data={data}")
    if data.name is not None and data.name != ps.name:
        existing = (
            await db.execute(
                select(PosterSize.id).where(
                    PosterSize.name == data.name,
                    PosterSize.id != poster_size_id,
                    PosterSize.is_deleted.is_(False)
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            raise BusinessException(ErrorCode.POSTER_SIZE_NAME_EXISTS, get_msg("POSTER_SIZE_NAME_EXISTS"))
        ps.name = data.name
    if data.width is not None:
        ps.width = data.width
    if data.height is not None:
        ps.height = data.height
    if data.max_file_size_kb is not None:
        ps.max_file_size_kb = data.max_file_size_kb
    if data.mandatory is not None:
        ps.mandatory = data.mandatory
    if data.mapping_type is not None:
        ps.mapping_type = data.mapping_type
    if data.belongings is not None:
        await db.execute(
            PosterSizeBelonging.__table__.delete().where(PosterSizeBelonging.poster_size_id == ps.id)  # type: ignore[attr-defined]
        )
        for b in data.belongings:
            db.add(PosterSizeBelonging(poster_size_id=ps.id, belonging=b))
    if data.extensions is not None:
        await db.execute(
            PosterSizeExtension.__table__.delete().where(PosterSizeExtension.poster_size_id == ps.id)  # type: ignore[attr-defined]
        )
        for e in data.extensions:
            db.add(PosterSizeExtension(poster_size_id=ps.id, extension=e))
    await db.commit()
    await db.refresh(ps)
    return ps


async def delete_poster_size(db: AsyncSession, poster_size_id: int) -> None:
    ps = await _get_poster_size_or_404(db, poster_size_id)
    logger.info(f"delete_poster_size 入参: poster_size_id={poster_size_id}")
    ps.is_deleted = True
    await db.commit()


async def batch_delete_poster_sizes(db: AsyncSession, req: BatchDeleteRequest) -> int:
    items = (await db.execute(
        select(PosterSize).where(PosterSize.id.in_(req.ids), PosterSize.is_deleted.is_(False))
    )).scalars().all()
    logger.info(f"batch_delete_poster_sizes 入参: req={req}")
    for ps in items:
        ps.is_deleted = True
    await db.commit()
    return len(items)
