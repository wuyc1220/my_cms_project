"""
媒资实体 API 路由层。

路由前缀：/movies

接口列表：
    GET    /movies/{movie_id}                        查询单个媒资
    GET    /contents/{content_id}/movies              查询内容关联的媒资列表
    POST   /contents/{content_id}/movies              创建媒资
    PUT    /movies/{movie_id}                         更新媒资
    DELETE /movies/{movie_id}                         删除媒资
    GET    /contents/{content_id}/movies/history      查询内容关联的媒资操作历史
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.internal.cms_biz_orchestration.schemas.movie import (
    MovieCreate,
    MovieItem,
    MovieUpdate,
    MovieListItem,
)
from app.internal.cms_biz_orchestration.schemas.movie_history import (
    MovieHistoryListItem,
)
from app.internal.cms_biz_orchestration.services import movie_service
from app.internal.cms_biz_metada.schemas.basic import (
    EntityFieldValueItem,
    EntityFieldValuesPayload,
    EntityI18nItem,
    EntityI18nPayload,
)
from app.internal.cms_biz_metada.services.entity_data_service import (
    get_field_values,
    save_field_values,
    get_i18n_values,
    save_i18n_values,
)

router = APIRouter(prefix="/movies", tags=["媒资管理"])


@router.get("/{movie_id}", response_model=MovieItem)
async def get_movie(
    movie_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询单个媒资实体。"""
    return await movie_service.get_movie(db, movie_id)


@router.get("/contents/{content_id}/movies", response_model=MovieListItem)
async def list_content_movies(
    content_id: int,
    movie_type: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询内容关联的媒资列表。"""
    return await movie_service.list_movies(db, content_id, movie_type)


@router.post("/contents/{content_id}/movies", response_model=MovieItem)
async def create_movie(
    content_id: int,
    data: MovieCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建媒资实体。"""
    data_dict = data.model_dump()
    data_dict["content_id"] = content_id
    result = await movie_service.create_movie(
        db, MovieCreate(**data_dict), processed_by=current_user.username, processed_by_id=current_user.id
    )
    new_data = orm_to_dict(result, "movie")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "movie", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MOVIE_CREATE,
        operation_object=f"媒资 {result.file_name}",
        operation_content=f"log.movie.create:{result.movie_type}",
        content_id=content_id,
        entity_type="movie",
        entity_id=result.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.put("/{movie_id}", response_model=MovieItem)
async def update_movie(
    movie_id: int,
    data: MovieUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新媒资实体。"""
    import copy
    old = await movie_service.get_movie(db, movie_id)
    old_data = copy.deepcopy(orm_to_dict(old, "movie")) if old else {}
    result = await movie_service.update_movie(db, movie_id, data)
    new_data = orm_to_dict(result, "movie")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "movie", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.MOVIE_UPDATE,
            operation_object=f"媒资 {result.file_name}",
            operation_content=f"log.movie.edit:{result.movie_type}",
            content_id=result.content_id,
            entity_type="movie",
            entity_id=result.id,
            previous_value=prev_val,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{movie_id}")
async def delete_movie(
    movie_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除媒资实体。"""
    existing = await movie_service.get_movie(db, movie_id)
    old_data = orm_to_dict(existing, "movie") if existing else {}
    await movie_service.delete_movie(db, movie_id, processed_by=current_user.username)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "movie", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MOVIE_DELETE,
        operation_object=f"媒资 {existing.file_name}",
        operation_content=f"log.movie.delete:{existing.movie_type}",
        content_id=existing.content_id,
        entity_type="movie",
        entity_id=existing.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/contents/{content_id}/movies/history", response_model=MovieHistoryListItem)
async def list_content_movie_history(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询内容关联的媒资操作历史列表。"""
    return await movie_service.list_movie_history(db, content_id)


# ── 自定义字段值 ────────────────────────────────────────

ENTITY_TYPE = "movie"


@router.get("/{movie_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_movie_field_values(
    movie_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询媒资实体的自定义字段值。"""
    await movie_service.get_movie(db, movie_id)
    return await get_field_values(db, ENTITY_TYPE, movie_id)


@router.put("/{movie_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_movie_field_values(
    movie_id: int,
    body: EntityFieldValuesPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """保存媒资实体的自定义字段值。"""
    await movie_service.get_movie(db, movie_id)
    return await save_field_values(db, ENTITY_TYPE, movie_id, body)


# ── 多语言值 ────────────────────────────────────────────


@router.get("/{movie_id}/i18n", response_model=list[EntityI18nItem])
async def get_movie_i18n(
    movie_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询媒资实体的多语言值。"""
    await movie_service.get_movie(db, movie_id)
    return await get_i18n_values(db, ENTITY_TYPE, movie_id)


@router.put("/{movie_id}/i18n", response_model=list[EntityI18nItem])
async def save_movie_i18n(
    movie_id: int,
    body: EntityI18nPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """保存媒资实体的多语言值（一次传一种语言的所有字段）。"""
    await movie_service.get_movie(db, movie_id)
    return await save_i18n_values(db, ENTITY_TYPE, movie_id, body)
