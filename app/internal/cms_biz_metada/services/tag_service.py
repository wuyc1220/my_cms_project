from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import Tag
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import TagCreate, TagListItem, TagUpdate
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


async def _get_tag_or_404(db: AsyncSession, tag_id: int) -> Tag:
    """获取标签，不存在则404"""
    tag = (await db.execute(
        select(Tag).where(Tag.id == tag_id, Tag.is_deleted.is_(False))
    )).scalar_one_or_none()
    if not tag:
        raise NotFoundException(ErrorCode.TAG_NOT_FOUND, get_msg("TAG_NOT_FOUND"))
    return tag


async def list_tags(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    languages: list[str] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[TagListItem]:
    query = select(Tag).where(Tag.is_deleted.is_(False))
    if name:
        query = query.where(Tag.name.ilike(f"%{name}%"))
    if languages:
        query = query.where(Tag.language.in_(languages))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Tag, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Tag.id.desc())
        else:
            query = query.order_by(Tag.id.desc())
    else:
        query = query.order_by(Tag.id.desc())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    tags = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[TagListItem.model_validate(t) for t in tags],
    )


async def get_tag(db: AsyncSession, tag_id: int) -> Tag:
    return await _get_tag_or_404(db, tag_id)


async def create_tag(db: AsyncSession, data: TagCreate) -> Tag:
    existing = (
        await db.execute(
            select(Tag.id).where(Tag.name == data.name, Tag.language == data.language, Tag.is_deleted.is_(False)).limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.TAG_NAME_EXISTS, get_msg("TAG_NAME_EXISTS"))
    tag = Tag(name=data.name, language=data.language)
    db.add(tag)
    await db.commit()
    await db.refresh(tag)
    return tag


async def update_tag(db: AsyncSession, tag_id: int, data: TagUpdate) -> Tag:
    tag = await _get_tag_or_404(db, tag_id)
    new_name = data.name if data.name is not None else tag.name
    new_language = data.language if data.language is not None else tag.language
    if new_name != tag.name or new_language != tag.language:
        existing = (
            await db.execute(
                select(Tag.id).where(
                    Tag.name == new_name,
                    Tag.language == new_language,
                    Tag.id != tag_id,
                    Tag.is_deleted.is_(False)
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            raise BusinessException(ErrorCode.TAG_NAME_EXISTS, get_msg("TAG_NAME_EXISTS"))
    if data.name is not None:
        tag.name = data.name
    if data.language is not None:
        tag.language = data.language
    await db.commit()
    await db.refresh(tag)
    return tag


async def delete_tag(db: AsyncSession, tag_id: int) -> None:
    tag = await _get_tag_or_404(db, tag_id)
    tag.is_deleted = True
    await db.commit()


async def batch_delete_tags(db: AsyncSession, req: BatchDeleteRequest) -> int:
    tags = (await db.execute(
        select(Tag).where(Tag.id.in_(req.ids), Tag.is_deleted.is_(False))
    )).scalars().all()
    for tag in tags:
        tag.is_deleted = True
    await db.commit()
    return len(tags)
