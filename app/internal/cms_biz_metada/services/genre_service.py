from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import Genre
from app.internal.cms_biz_system.models.dict import DictNode
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import GenreCreate, GenreListItem, GenreUpdate
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


async def _get_default_language(db: AsyncSession) -> str | None:
    """获取 Multi_Languages 字典表的第一种语言（默认语言）"""
    root = (
        await db.execute(
            select(DictNode).where(DictNode.parent_id.is_(None), DictNode.code == "Multi_Languages", DictNode.is_deleted == False)
        )
    ).scalar_one_or_none()
    if root is None:
        return None

    first_lang = (
        await db.execute(
            select(DictNode)
            .where(DictNode.parent_id == root.id, DictNode.status == "active", DictNode.is_deleted == False)
            .order_by(DictNode.sort_order, DictNode.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return first_lang.code if first_lang else None


async def _get_genre_or_404(db: AsyncSession, genre_id: int) -> Genre:
    """获取题材，不存在则404"""
    genre = (await db.execute(
        select(Genre).where(Genre.id == genre_id, Genre.is_deleted.is_(False))
    )).scalar_one_or_none()
    if not genre:
        raise NotFoundException(ErrorCode.GENRE_NOT_FOUND, get_msg("GENRE_NOT_FOUND"))
    return genre


async def list_genres(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    languages: list[str] | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[GenreListItem]:
    query = select(Genre).where(Genre.is_deleted.is_(False))
    if name:
        query = query.where(Genre.name.ilike(f"%{name}%"))

    if languages:
        query = query.where(Genre.language.in_(languages))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(Genre, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Genre.id.desc())
        else:
            query = query.order_by(Genre.id.desc())
    else:
        query = query.order_by(Genre.id.desc())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    genres = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[GenreListItem.model_validate(g) for g in genres],
    )


async def get_genre(db: AsyncSession, genre_id: int) -> Genre:
    return await _get_genre_or_404(db, genre_id)


async def create_genre(db: AsyncSession, data: GenreCreate) -> Genre:
    existing = (
        await db.execute(
            select(Genre.id).where(
                Genre.name == data.name,
                Genre.language == data.language,
                Genre.is_deleted.is_(False)
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.GENRE_NAME_EXISTS, get_msg("GENRE_NAME_EXISTS"))
    genre = Genre(name=data.name, language=data.language)
    db.add(genre)
    await db.commit()
    await db.refresh(genre)
    return genre


async def update_genre(db: AsyncSession, genre_id: int, data: GenreUpdate) -> Genre:
    genre = await _get_genre_or_404(db, genre_id)
    new_name = data.name if data.name is not None else genre.name
    new_language = data.language if data.language is not None else genre.language
    if new_name != genre.name or new_language != genre.language:
        existing = (
            await db.execute(
                select(Genre.id).where(
                    Genre.name == new_name,
                    Genre.language == new_language,
                    Genre.id != genre_id,
                    Genre.is_deleted.is_(False)
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing:
            raise BusinessException(ErrorCode.GENRE_NAME_EXISTS, get_msg("GENRE_NAME_EXISTS"))
    if data.name is not None:
        genre.name = data.name
    if data.language is not None:
        genre.language = data.language
    await db.commit()
    await db.refresh(genre)
    return genre


async def delete_genre(db: AsyncSession, genre_id: int) -> None:
    genre = await _get_genre_or_404(db, genre_id)
    genre.is_deleted = True
    await db.commit()


async def batch_delete_genres(db: AsyncSession, req: BatchDeleteRequest) -> int:
    genres = (await db.execute(
        select(Genre).where(Genre.id.in_(req.ids), Genre.is_deleted.is_(False))
    )).scalars().all()
    for genre in genres:
        genre.is_deleted = True
    await db.commit()
    return len(genres)
