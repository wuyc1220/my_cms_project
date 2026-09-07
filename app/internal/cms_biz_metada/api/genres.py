from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_metada.schemas.basic import GenreCreate, GenreListItem, GenreUpdate
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.internal.cms_biz_metada.services.genre_service import batch_delete_genres, create_genre, delete_genre, get_genre, list_genres, update_genre
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values

router = APIRouter(prefix="/genres")


@router.get("/", response_model=PaginatedResponse[GenreListItem])
async def get_genre_list(
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    languages: list[str] | None = Query(default=None),
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from loguru import logger
    logger.info(f"[Genre] 查询参数: page={page}, page_size={page_size}, name={name}, languages={languages}, sort_by={sort_by}, sort_order={sort_order}")
    return await list_genres(db, page, page_size, name, languages, sort_by, sort_order)


@router.get("/{genre_id}", response_model=GenreListItem)
async def get_genre_detail(
    genre_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return GenreListItem.model_validate(await get_genre(db, genre_id))


@router.post("/", response_model=GenreListItem)
async def create_genre_api(
    body: GenreCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    genre = await create_genre(db, body)
    new_data = orm_to_dict(genre)
    prev_val, new_val, raw_val = await prepare_log_values(db, "genre", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.GENRE_CREATE,
        operation_object_code="OBJ_GENRE", operation_object_params={"name": body.name},
        operation_content_code="LOG_GENRE_CREATE", operation_content_params={"name": body.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="genre",
        entity_id=genre.id,
    )
    await db.commit()
    return GenreListItem.model_validate(genre)


@router.put("/{genre_id}", response_model=GenreListItem)
async def update_genre_api(
    genre_id: int,
    body: GenreUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_genre(db, genre_id)
    old_data = orm_to_dict(old)
    old_name = old.name
    genre = await update_genre(db, genre_id, body)
    new_data = orm_to_dict(genre)
    prev_val, new_val, raw_val = await prepare_log_values(db, "genre", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.GENRE_EDIT,
        operation_object_code="OBJ_GENRE", operation_object_params={"name": old_name},
        operation_content_code="LOG_GENRE_EDIT", operation_content_params={"name": genre.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="genre",
        entity_id=genre_id,
    )
    await db.commit()
    return GenreListItem.model_validate(genre)


@router.delete("/batch")
async def batch_delete_genres_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Genre.name).where(Genre.id.in_(body.ids)))).scalars().all()
    genre_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await batch_delete_genres(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.GENRE_BATCH_DELETE,
        operation_object_code="OBJ_GENRE", operation_object_params={"name": genre_names},
        operation_content_code="LOG_GENRE_BATCH_DELETE", operation_content_params={"names": genre_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="genre",
        entity_id=body.ids[0] if body.ids else None,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.delete("/{genre_id}")
async def delete_genre_api(
    genre_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    genre = await get_genre(db, genre_id)
    genre_name = genre.name
    old_data = orm_to_dict(genre)
    prev_val, new_val, raw_val = await prepare_log_values(db, "genre", old_data, None)
    await delete_genre(db, genre_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.GENRE_DELETE,
        operation_object_code="OBJ_GENRE", operation_object_params={"name": genre_name},
        operation_content_code="LOG_GENRE_DELETE", operation_content_params={"name": genre_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="genre",
        entity_id=genre_id,
    )
    await db.commit()
    return {"success": True}
