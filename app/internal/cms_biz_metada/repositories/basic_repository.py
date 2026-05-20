"""
内容元数据模块 - 数据访问层
封装 Cast / Category / ContentType / Genre / Tag / CustomField / PosterSize / EntityData 的 SQLAlchemy 操作
"""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import (
    Cast,
    Category,
    ContentType,
    CustomField,
    CustomFieldOption,
    EntityTypeCustomField,
    Genre,
    PosterSize,
    Tag,
)


# ═══════════════════════════════════════════════════════════
# Cast
# ═══════════════════════════════════════════════════════════

async def get_cast_by_id(db: AsyncSession, cast_id: int) -> Cast | None:
    return (await db.execute(select(Cast).where(Cast.id == cast_id, Cast.is_deleted.is_(False)))).scalar_one_or_none()


async def list_casts_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    cast_id: int | None = None, name: str | None = None,
    description: str | None = None, ingest_statuses: list[str] | None = None,
    sort_by: str | None = None, sort_order: str | None = None,
) -> tuple[list[Cast], int]:
    query = select(Cast).where(Cast.is_deleted.is_(False))
    if cast_id is not None:
        query = query.where(Cast.id == cast_id)
    if name:
        query = query.where(Cast.name.ilike(f"%{name}%"))
    if description:
        query = query.where(Cast.description.ilike(f"%{description}%"))
    if ingest_statuses:
        query = query.where(Cast.ingest_status.in_(ingest_statuses))

    if sort_by and sort_order:
        sort_column = getattr(Cast, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc(), Cast.id.desc())
        else:
            query = query.order_by(Cast.id.desc())
    else:
        query = query.order_by(Cast.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_casts_by_ids(db: AsyncSession, ids: list[int]) -> list[Cast]:
    return (await db.execute(select(Cast).where(Cast.id.in_(ids), Cast.is_deleted.is_(False)))).scalars().all()


async def add_cast(db: AsyncSession, cast: Cast) -> None:
    db.add(cast)


# ═══════════════════════════════════════════════════════════
# Category
# ═══════════════════════════════════════════════════════════

async def get_category_by_id(db: AsyncSession, category_id: int) -> Category | None:
    return (await db.execute(select(Category).where(Category.id == category_id, Category.is_deleted.is_(False)))).scalar_one_or_none()


async def get_all_categories(db: AsyncSession) -> list[Category]:
    return (await db.execute(select(Category).where(Category.is_deleted.is_(False)).order_by(Category.sequence, Category.id))).scalars().all()


async def get_categories_by_ids(db: AsyncSession, ids: list[int]) -> list[Category]:
    return (await db.execute(select(Category).where(Category.id.in_(ids), Category.is_deleted.is_(False)))).scalars().all()


async def has_child_categories(db: AsyncSession, parent_id: int) -> bool:
    return (await db.execute(
        select(Category.id).where(Category.parent_id == parent_id, Category.is_deleted.is_(False)).limit(1)
    )).scalar_one_or_none() is not None


async def add_category(db: AsyncSession, cat: Category) -> None:
    db.add(cat)


async def delete_category(db: AsyncSession, cat: Category) -> None:
    await db.delete(cat)


# ═══════════════════════════════════════════════════════════
# ContentType
# ═══════════════════════════════════════════════════════════

async def get_content_type_by_id(db: AsyncSession, ct_id: int) -> ContentType | None:
    return (await db.execute(select(ContentType).where(ContentType.id == ct_id, ContentType.is_deleted.is_(False)))).scalar_one_or_none()


async def get_content_type_by_code(db: AsyncSession, code: str) -> ContentType | None:
    return (await db.execute(select(ContentType).where(ContentType.code == code, ContentType.is_deleted.is_(False)))).scalar_one_or_none()


async def list_content_types_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    name: str | None = None, code: str | None = None,
    sort_by: str | None = None, sort_order: str | None = None,
) -> tuple[list[ContentType], int]:
    query = select(ContentType).where(ContentType.is_deleted.is_(False))
    if name:
        query = query.where(ContentType.name.ilike(f"%{name}%"))
    if code:
        query = query.where(ContentType.code.ilike(f"%{code}%"))

    if sort_by and sort_order:
        sort_column = getattr(ContentType, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc())
        else:
            query = query.order_by(ContentType.id.desc())
    else:
        query = query.order_by(ContentType.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_all_content_types(db: AsyncSession) -> list[ContentType]:
    return (await db.execute(select(ContentType).where(ContentType.is_deleted.is_(False)).order_by(ContentType.id))).scalars().all()


async def add_content_type(db: AsyncSession, ct: ContentType) -> None:
    db.add(ct)


# ═══════════════════════════════════════════════════════════
# Genre
# ═══════════════════════════════════════════════════════════

async def get_genre_by_id(db: AsyncSession, genre_id: int) -> Genre | None:
    return (await db.execute(select(Genre).where(Genre.id == genre_id, Genre.is_deleted.is_(False)))).scalar_one_or_none()


async def list_genres_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    name: str | None = None, code: str | None = None,
    sort_by: str | None = None, sort_order: str | None = None,
) -> tuple[list[Genre], int]:
    query = select(Genre).where(Genre.is_deleted.is_(False))
    if name:
        query = query.where(Genre.name.ilike(f"%{name}%"))
    if code:
        query = query.where(Genre.code.ilike(f"%{code}%"))

    if sort_by and sort_order:
        sort_column = getattr(Genre, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc(), Genre.id.desc())
        else:
            query = query.order_by(Genre.id.desc())
    else:
        query = query.order_by(Genre.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_genres_by_ids(db: AsyncSession, ids: list[int]) -> list[Genre]:
    return (await db.execute(select(Genre).where(Genre.id.in_(ids), Genre.is_deleted.is_(False)))).scalars().all()


async def add_genre(db: AsyncSession, genre: Genre) -> None:
    db.add(genre)


# ═══════════════════════════════════════════════════════════
# Tag
# ═══════════════════════════════════════════════════════════

async def get_tag_by_id(db: AsyncSession, tag_id: int) -> Tag | None:
    return (await db.execute(select(Tag).where(Tag.id == tag_id, Tag.is_deleted.is_(False)))).scalar_one_or_none()


async def list_tags_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    name: str | None = None, group: str | None = None,
    sort_by: str | None = None, sort_order: str | None = None,
) -> tuple[list[Tag], int]:
    query = select(Tag).where(Tag.is_deleted.is_(False))
    if name:
        query = query.where(Tag.name.ilike(f"%{name}%"))
    if group:
        query = query.where(Tag.group.ilike(f"%{group}%"))

    if sort_by and sort_order:
        sort_column = getattr(Tag, sort_by, None)
        if sort_column is not None:
            query = query.order_by(sort_column.asc() if sort_order == 'asc' else sort_column.desc(), Tag.id.desc())
        else:
            query = query.order_by(Tag.id.desc())
    else:
        query = query.order_by(Tag.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_all_tags(db: AsyncSession) -> list[Tag]:
    return (await db.execute(select(Tag).where(Tag.is_deleted.is_(False)).order_by(Tag.id))).scalars().all()


async def get_tags_by_ids(db: AsyncSession, ids: list[int]) -> list[Tag]:
    return (await db.execute(select(Tag).where(Tag.id.in_(ids), Tag.is_deleted.is_(False)))).scalars().all()


async def add_tag(db: AsyncSession, tag: Tag) -> None:
    db.add(tag)


# ═══════════════════════════════════════════════════════════
# CustomField
# ═══════════════════════════════════════════════════════════

async def get_custom_field_by_id(db: AsyncSession, cf_id: int) -> CustomField | None:
    return (await db.execute(select(CustomField).where(CustomField.id == cf_id, CustomField.is_deleted.is_(False)))).scalar_one_or_none()


async def list_custom_fields_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    name: str | None = None, field_type: str | None = None,
    entity_type: str | None = None,
) -> tuple[list[CustomField], int]:
    query = select(CustomField).where(CustomField.is_deleted.is_(False))
    if name:
        query = query.where(CustomField.name.ilike(f"%{name}%"))
    if field_type:
        query = query.where(CustomField.field_type == field_type)
    if entity_type:
        query = query.where(CustomField.entity_type == entity_type)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.order_by(CustomField.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def get_all_custom_fields(db: AsyncSession) -> list[CustomField]:
    return (await db.execute(select(CustomField).where(CustomField.is_deleted.is_(False)).order_by(CustomField.id))).scalars().all()


async def add_custom_field(db: AsyncSession, cf: CustomField) -> None:
    db.add(cf)


async def get_custom_field_options(db: AsyncSession, custom_field_id: int) -> list[CustomFieldOption]:
    return (await db.execute(
        select(CustomFieldOption).where(CustomFieldOption.custom_field_id == custom_field_id)
    )).scalars().all()


async def add_custom_field_option(db: AsyncSession, option: CustomFieldOption) -> None:
    db.add(option)


async def get_entity_type_custom_fields(db: AsyncSession, entity_type: str) -> list[EntityTypeCustomField]:
    return (await db.execute(
        select(EntityTypeCustomField).where(EntityTypeCustomField.entity_type == entity_type)
    )).scalars().all()


# ═══════════════════════════════════════════════════════════
# PosterSize
# ═══════════════════════════════════════════════════════════

async def get_poster_size_by_id(db: AsyncSession, ps_id: int) -> PosterSize | None:
    return (await db.execute(select(PosterSize).where(PosterSize.id == ps_id, PosterSize.is_deleted.is_(False)))).scalar_one_or_none()


async def list_poster_sizes_query(
    db: AsyncSession, *, page: int = 1, page_size: int = 10,
    name: str | None = None, platform: str | None = None,
) -> tuple[list[PosterSize], int]:
    query = select(PosterSize).where(PosterSize.is_deleted.is_(False))
    if name:
        query = query.where(PosterSize.name.ilike(f"%{name}%"))
    if platform:
        query = query.where(PosterSize.platform == platform)

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    result = await db.execute(query.order_by(PosterSize.id.desc()).offset((page - 1) * page_size).limit(page_size))
    return result.scalars().all(), total


async def add_poster_size(db: AsyncSession, ps: PosterSize) -> None:
    db.add(ps)
