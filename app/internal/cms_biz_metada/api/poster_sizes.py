from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_metada.models.basic import PosterSize
from app.internal.cms_biz_metada.schemas.basic import (
    EntityFieldValueItem,
    EntityFieldValuesPayload,
    EntityI18nItem,
    EntityI18nPayload,
    PosterSizeCreate,
    PosterSizeListItem,
    PosterSizeUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.internal.cms_biz_metada.services.poster_size_service import (
    batch_delete_poster_sizes,
    create_poster_size,
    delete_poster_size,
    get_poster_size,
    list_poster_sizes,
    update_poster_size,
)
from app.internal.cms_biz_metada.services.entity_data_service import (
    get_field_values,
    save_field_values,
    get_i18n_values,
    save_i18n_values,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values

router = APIRouter(prefix="/poster-sizes")

ENTITY_TYPE = "poster_size"


@router.get("/", response_model=PaginatedResponse[PosterSizeListItem])
async def get_poster_size_list(
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    belongings: list[str] | None = Query(default=None),
    mandatory: bool | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_poster_sizes(db, page, page_size, name, belongings, mandatory, sort_by, sort_order)


@router.get("/{poster_size_id}", response_model=PosterSizeListItem)
async def get_poster_size_detail(
    poster_size_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return PosterSizeListItem.from_orm(await get_poster_size(db, poster_size_id))


@router.post("/", response_model=PosterSizeListItem)
async def create_poster_size_api(
    body: PosterSizeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ps = await create_poster_size(db, body)
    new_data = orm_to_dict(ps)
    prev_val, new_val, raw_val = await prepare_log_values(db, "poster_size", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.POSTER_SIZE_CREATE,
        operation_object_code="OBJ_POSTER_SIZE", operation_object_params={"name": body.name},
        operation_content_code="LOG_POSTER_SIZE_CREATE", operation_content_params={"name": body.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="poster_size",
        entity_id=ps.id,
    )
    await db.commit()
    return PosterSizeListItem.from_orm(ps)


@router.put("/{poster_size_id}", response_model=PosterSizeListItem)
async def update_poster_size_api(
    poster_size_id: int,
    body: PosterSizeUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_poster_size(db, poster_size_id)
    old_name = old.name
    old_data = orm_to_dict(old)
    ps = await update_poster_size(db, poster_size_id, body)
    new_data = orm_to_dict(ps)
    prev_val, new_val, raw_val = await prepare_log_values(db, "poster_size", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.POSTER_SIZE_EDIT,
        operation_object_code="OBJ_POSTER_SIZE", operation_object_params={"name": old_name},
        operation_content_code="LOG_POSTER_SIZE_EDIT", operation_content_params={"name": ps.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="poster_size",
        entity_id=poster_size_id,
    )
    await db.commit()
    return PosterSizeListItem.from_orm(ps)


@router.delete("/batch")
async def batch_delete_poster_sizes_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(PosterSize.name).where(PosterSize.id.in_(body.ids)))).scalars().all()
    ps_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await batch_delete_poster_sizes(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.POSTER_SIZE_BATCH_DELETE,
        operation_object_code="OBJ_POSTER_SIZE", operation_object_params={"name": ps_names},
        operation_content_code="LOG_POSTER_SIZE_BATCH_DELETE", operation_content_params={"names": ps_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="poster_size",
        entity_id=body.ids[0] if body.ids else None,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.delete("/{poster_size_id}")
async def delete_poster_size_api(
    poster_size_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ps = await get_poster_size(db, poster_size_id)
    ps_name = ps.name
    old_data = orm_to_dict(ps)
    prev_val, new_val, raw_val = await prepare_log_values(db, "poster_size", old_data, None)
    await delete_poster_size(db, poster_size_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.POSTER_SIZE_DELETE,
        operation_object_code="OBJ_POSTER_SIZE", operation_object_params={"name": ps_name},
        operation_content_code="LOG_POSTER_SIZE_DELETE", operation_content_params={"name": ps_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="poster_size",
        entity_id=poster_size_id,
    )
    await db.commit()
    return {"success": True}


# ---------- Custom Field Values ----------

@router.get("/{poster_size_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_poster_size_field_values(
    poster_size_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await get_poster_size(db, poster_size_id)
    return await get_field_values(db, ENTITY_TYPE, poster_size_id)


@router.put("/{poster_size_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_poster_size_field_values(
    poster_size_id: int,
    body: EntityFieldValuesPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await get_poster_size(db, poster_size_id)
    return await save_field_values(db, ENTITY_TYPE, poster_size_id, body)


# ---------- Multi-Language Values ----------

@router.get("/{poster_size_id}/i18n", response_model=list[EntityI18nItem])
async def get_poster_size_i18n(
    poster_size_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询单个海报规格的多语言字段值。"""
    await get_poster_size(db, poster_size_id)
    return await get_i18n_values(db, ENTITY_TYPE, poster_size_id)


@router.put("/{poster_size_id}/i18n", response_model=list[EntityI18nItem])
async def save_poster_size_i18n(
    poster_size_id: int,
    body: EntityI18nPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """保存单个海报规格的多语言字段值。"""
    await get_poster_size(db, poster_size_id)
    return await save_i18n_values(db, ENTITY_TYPE, poster_size_id, body)


@router.get("/{poster_size_id}/history")
async def get_poster_size_history(
    poster_size_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from app.internal.cms_biz_system.services.operation_log_service import list_entity_history
    return await list_entity_history(db, "poster_size", poster_size_id, limit)
