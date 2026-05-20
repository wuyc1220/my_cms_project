from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import CustomTag
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import CustomTagCreate, CustomTagListItem, CustomTagUpdate
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


async def list_custom_tags(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    languages: list[str] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[CustomTagListItem]:
    query = select(CustomTag).where(CustomTag.is_deleted == False)
    if name:
        query = query.where(CustomTag.name.ilike(f"%{name}%"))

    if languages:
        query = query.where(CustomTag.language.in_(languages))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(CustomTag, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, CustomTag.id.desc())
        else:
            query = query.order_by(CustomTag.id.desc())
    else:
        query = query.order_by(CustomTag.id.desc())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    tags = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[CustomTagListItem.model_validate(t) for t in tags],
    )


async def get_custom_tag(db: AsyncSession, tag_id: int) -> CustomTag:
    tag = (await db.execute(select(CustomTag).where(CustomTag.id == tag_id, CustomTag.is_deleted == False))).scalar_one_or_none()
    if not tag:
        raise NotFoundException(ErrorCode.CUSTOM_TAG_NOT_FOUND, get_msg("CUSTOM_TAG_NOT_FOUND"))
    return tag


async def create_custom_tag(db: AsyncSession, data: CustomTagCreate) -> CustomTag:
    existing = (
        await db.execute(select(CustomTag).where(CustomTag.name == data.name, CustomTag.is_deleted == False))
    ).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.CUSTOM_TAG_NAME_EXISTS, get_msg("CUSTOM_TAG_NAME_EXISTS"))
    tag = CustomTag(name=data.name, language="default")
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


async def update_custom_tag(db: AsyncSession, tag_id: int, data: CustomTagUpdate) -> CustomTag:
    tag = await get_custom_tag(db, tag_id)
    new_name = data.name if data.name is not None else tag.name
    if new_name != tag.name:
        existing = (
            await db.execute(
                select(CustomTag).where(CustomTag.name == new_name, CustomTag.is_deleted == False, CustomTag.id != tag_id)
            )
        ).scalar_one_or_none()
        if existing:
            raise BusinessException(ErrorCode.CUSTOM_TAG_NAME_EXISTS, get_msg("CUSTOM_TAG_NAME_EXISTS"))
    if data.name is not None:
        tag.name = data.name
    await db.commit()
    await db.refresh(tag)
    return tag


async def delete_custom_tag(db: AsyncSession, tag_id: int) -> None:
    tag = await get_custom_tag(db, tag_id)
    tag.is_deleted = True
    await db.commit()


async def batch_delete_custom_tags(db: AsyncSession, req: BatchDeleteRequest) -> int:
    tags = (await db.execute(select(CustomTag).where(CustomTag.id.in_(req.ids), CustomTag.is_deleted == False))).scalars().all()
    for tag in tags:
        tag.is_deleted = True
    await db.commit()
    return len(tags)
