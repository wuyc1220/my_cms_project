from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_metada.models.basic import Cast
from app.internal.cms_biz_metada.schemas.basic import (
    CastCreate,
    CastListItem,
    CastUpdate,
    EntityFieldValueItem,
    EntityFieldValuesPayload,
    EntityI18nItem,
    EntityI18nPayload,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.internal.cms_biz_metada.services.cast_service import (
    batch_delete_casts,
    create_cast,
    delete_cast,
    get_cast,
    list_casts,
    update_cast,
)
from app.internal.cms_biz_metada.services.entity_data_service import (
    get_field_values,
    get_i18n_values,
    save_field_values,
    save_i18n_values,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values

router = APIRouter(prefix="/casts")

ENTITY_TYPE = "cast"


@router.get("/", response_model=PaginatedResponse[CastListItem])
async def get_cast_list(
    page: int = 1,
    page_size: int = 10,
    cast_id: int | None = None,
    name: str | None = None,
    description: str | None = None,
    ingest_statuses: list[str] | None = Query(default=None),
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_casts(db, page, page_size, cast_id, name, description, ingest_statuses, sort_by, sort_order)


@router.post("/", response_model=CastListItem)
async def create_cast_api(
    body: CastCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cast = await create_cast(db, body)
    new_data = orm_to_dict(cast)
    prev_val, new_val, raw_val = await prepare_log_values(db, "cast", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CAST_CREATE,
        operation_object=f"人物 {body.name}",
        operation_content=f"Created cast: name={body.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="cast",
        entity_id=cast.id,
    )
    await db.commit()
    return CastListItem.model_validate(cast)


@router.delete("/batch")
async def batch_delete_casts_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Cast.name).where(Cast.id.in_(body.ids)))).scalars().all()
    cast_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await batch_delete_casts(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CAST_BATCH_DELETE,
        operation_object=f"人物 {cast_names}",
        operation_content=f"批量删除人物: {cast_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="cast",
        entity_id=body.ids[0] if body.ids else None,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.get("/{cast_id}", response_model=CastListItem)
async def get_cast_detail(
    cast_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return CastListItem.model_validate(await get_cast(db, cast_id))


@router.put("/{cast_id}", response_model=CastListItem)
async def update_cast_api(
    cast_id: int,
    body: CastUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_cast(db, cast_id)
    old_data = orm_to_dict(old)
    cast = await update_cast(db, cast_id, body)
    new_data = orm_to_dict(cast)
    prev_val, new_val, raw_val = await prepare_log_values(db, "cast", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CAST_EDIT,
        operation_object=f"人物 {old.name}",
        operation_content=f"Updated cast: ID={cast_id}, name={cast.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="cast",
        entity_id=cast_id,
    )
    await db.commit()
    return CastListItem.model_validate(cast)


@router.delete("/{cast_id}")
async def delete_cast_api(
    cast_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cast = await get_cast(db, cast_id)
    cast_name = cast.name
    old_data = orm_to_dict(cast)
    prev_val, new_val, raw_val = await prepare_log_values(db, "cast", old_data, None)
    await delete_cast(db, cast_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CAST_DELETE,
        operation_object=f"人物 {cast_name}",
        operation_content=f"Deleted cast: ID={cast_id}, name={cast_name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="cast",
        entity_id=cast_id,
    )
    await db.commit()
    return {"success": True}


@router.get("/{cast_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_cast_field_values(
    cast_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await get_cast(db, cast_id)
    return await get_field_values(db, ENTITY_TYPE, cast_id)


@router.put("/{cast_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_cast_field_values(
    cast_id: int,
    body: EntityFieldValuesPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await get_cast(db, cast_id)
    return await save_field_values(db, ENTITY_TYPE, cast_id, body)


@router.get("/{cast_id}/i18n", response_model=list[EntityI18nItem])
async def get_cast_i18n(
    cast_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await get_cast(db, cast_id)
    return await get_i18n_values(db, ENTITY_TYPE, cast_id)


@router.put("/{cast_id}/i18n", response_model=list[EntityI18nItem])
async def save_cast_i18n(
    cast_id: int,
    body: EntityI18nPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await get_cast(db, cast_id)
    return await save_i18n_values(db, ENTITY_TYPE, cast_id, body)


@router.get("/{cast_id}/history")
async def get_cast_history(
    cast_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from app.internal.cms_biz_system.services.operation_log_service import list_entity_history
    return await list_entity_history(db, "cast", cast_id, limit)
