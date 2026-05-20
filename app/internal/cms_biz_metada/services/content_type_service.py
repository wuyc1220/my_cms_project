from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import ContentType
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import ContentTypeCreate, ContentTypeListItem, ContentTypeUpdate
from app.common.schemas import PaginatedResponse
from loguru import logger
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


async def list_content_types(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[ContentTypeListItem]:
    query = select(ContentType).where(ContentType.is_deleted.is_(False))
    logger.info(f"list_content_types 入参: page={page}, page_size={page_size}, name={name}, sort_by={sort_by}, sort_order={sort_order}")
    if name:
        query = query.where(ContentType.name.ilike(f"%{name}%"))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(ContentType, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, ContentType.id.desc())
        else:
            query = query.order_by(ContentType.id.desc())
    else:
        query = query.order_by(ContentType.id.desc())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    types = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    logger.info(f"list_content_types 出参: total={total}, count={len(types)}")
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[ContentTypeListItem.model_validate(t) for t in types],
    )


async def get_content_type(db: AsyncSession, type_id: int) -> ContentType:
    ct = (await db.execute(select(ContentType).where(ContentType.id == type_id, ContentType.is_deleted.is_(False)))).scalar_one_or_none()
    logger.info(f"get_content_type 入参: type_id={type_id}")
    if not ct:
        raise NotFoundException(ErrorCode.CONTENT_TYPE_NOT_FOUND, get_msg("CONTENT_TYPE_NOT_FOUND"))
    return ct


async def create_content_type(db: AsyncSession, data: ContentTypeCreate) -> ContentType:
    existing = (await db.execute(select(ContentType).where(ContentType.name == data.name, ContentType.is_deleted.is_(False)))).scalar_one_or_none()
    logger.info(f"create_content_type 入参: data={data}")
    if existing:
        raise BusinessException(ErrorCode.CONTENT_TYPE_NAME_EXISTS, get_msg("CONTENT_TYPE_NAME_EXISTS"))
    ct = ContentType(name=data.name)
    db.add(ct)
    await db.commit()
    await db.refresh(ct)
    return ct


async def update_content_type(db: AsyncSession, type_id: int, data: ContentTypeUpdate) -> ContentType:
    ct = await get_content_type(db, type_id)
    logger.info(f"update_content_type 入参: type_id={type_id}, data={data}")
    if data.name is not None and data.name != ct.name:
        existing = (
            await db.execute(select(ContentType).where(ContentType.name == data.name, ContentType.id != type_id, ContentType.is_deleted.is_(False)))
        ).scalar_one_or_none()
        if existing:
            raise BusinessException(ErrorCode.CONTENT_TYPE_NAME_EXISTS, get_msg("CONTENT_TYPE_NAME_EXISTS"))
        ct.name = data.name
    await db.commit()
    await db.refresh(ct)
    return ct


async def delete_content_type(db: AsyncSession, type_id: int) -> None:
    ct = await get_content_type(db, type_id)
    logger.info(f"delete_content_type 入参: type_id={type_id}")
    ct.is_deleted = True
    await db.commit()


async def batch_delete_content_types(db: AsyncSession, req: BatchDeleteRequest) -> int:
    types = (await db.execute(select(ContentType).where(ContentType.id.in_(req.ids), ContentType.is_deleted.is_(False)))).scalars().all()
    logger.info(f"batch_delete_content_types 入参: req={req}")
    for ct in types:
        ct.is_deleted = True
    await db.commit()
    return len(types)
