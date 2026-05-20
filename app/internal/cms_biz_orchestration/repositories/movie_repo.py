"""
媒资实体 Repository 层。

封装 Movie 表的 SQLAlchemy 查询操作。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.movie import Movie


async def get_movie_by_id(db: AsyncSession, movie_id: int) -> Movie | None:
    """按 ID 查询媒资实体。"""
    return (
        await db.execute(select(Movie).where(Movie.id == movie_id, Movie.is_deleted.is_(False), Movie.is_discarded.is_(False)))
    ).scalar_one_or_none()


async def list_movies_by_content_id(
    db: AsyncSession, content_id: int, movie_type: int | None = None
) -> list[Movie]:
    """按 content_id 查询媒资列表，可选按类型过滤。"""
    query = select(Movie).where(Movie.content_id == content_id, Movie.is_deleted.is_(False), Movie.is_discarded.is_(False))
    if movie_type is not None:
        query = query.where(Movie.movie_type == movie_type)
    return (await db.execute(query.order_by(Movie.id.asc()))).scalars().all()


async def add_movie(db: AsyncSession, movie: Movie) -> None:
    """新增媒资实体（flush 以获取数据库生成的主键 id）。"""
    db.add(movie)
    await db.flush()


async def delete_movie(db: AsyncSession, movie_id: int) -> bool:
    """删除媒资实体（软删除）。"""
    movie = await get_movie_by_id(db, movie_id)
    if movie:
        movie.is_deleted = True
        return True
    return False
