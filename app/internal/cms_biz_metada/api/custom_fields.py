from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_metada.models.basic import CustomField
from app.internal.cms_biz_metada.schemas.basic import CustomFieldCreate, CustomFieldListItem, CustomFieldUpdate
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.internal.cms_biz_metada.services.custom_field_service import (
    batch_delete_custom_fields,
    create_custom_field,
    delete_custom_field,
    get_custom_field,
    list_custom_fields,
    update_custom_field,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values

router = APIRouter(prefix="/custom-fields")


@router.get("/", response_model=PaginatedResponse[CustomFieldListItem])
async def get_custom_field_list(
    page: int = 1,
    page_size: int = 10,
    field_name: str | None = None,
    field_type: str | None = None,
    belongings: list[str] | None = Query(default=None),
    mandatory: bool | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_custom_fields(db, page, page_size, field_name, field_type, belongings, mandatory, sort_by, sort_order)


@router.get("/{field_id}", response_model=CustomFieldListItem)
async def get_custom_field_detail(
    field_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return CustomFieldListItem.from_orm(await get_custom_field(db, field_id))


@router.post("/", response_model=CustomFieldListItem)
async def create_custom_field_api(
    body: CustomFieldCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cf = await create_custom_field(db, body)
    new_data = orm_to_dict(cf)
    prev_val, new_val, raw_val = await prepare_log_values(db, "custom_field", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CUSTOM_FIELD_CREATE,
        operation_object_code="OBJ_CUSTOM_FIELD", operation_object_params={"name": body.field_name},
        operation_content_code="LOG_CUSTOM_FIELD_CREATE", operation_content_params={"name": body.field_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="custom_field",
        entity_id=cf.id,
    )
    await db.commit()
    return CustomFieldListItem.from_orm(cf)


@router.put("/{field_id}", response_model=CustomFieldListItem)
async def update_custom_field_api(
    field_id: int,
    body: CustomFieldUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_custom_field(db, field_id)
    old_name = old.field_name
    old_data = orm_to_dict(old)
    cf = await update_custom_field(db, field_id, body)
    new_data = orm_to_dict(cf)
    prev_val, new_val, raw_val = await prepare_log_values(db, "custom_field", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CUSTOM_FIELD_EDIT,
        operation_object_code="OBJ_CUSTOM_FIELD", operation_object_params={"name": old_name},
        operation_content_code="LOG_CUSTOM_FIELD_EDIT", operation_content_params={"name": cf.field_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="custom_field",
        entity_id=field_id,
    )
    await db.commit()
    return CustomFieldListItem.from_orm(cf)


@router.delete("/batch")
async def batch_delete_custom_fields_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    fields = (await db.execute(select(CustomField).where(CustomField.id.in_(body.ids), CustomField.is_deleted.is_(False)))).scalars().all()
    field_names = ", ".join([f.field_name for f in fields]) if fields else str(body.ids)
    prev_data = [orm_to_dict(f) for f in fields]
    prev_val, _, raw_val = await prepare_log_values(db, "custom_field", prev_data, None)
    deleted = await batch_delete_custom_fields(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CUSTOM_FIELD_BATCH_DELETE,
        operation_object_code="OBJ_CUSTOM_FIELD", operation_object_params={"name": field_names},
        operation_content_code="LOG_CUSTOM_FIELD_BATCH_DELETE", operation_content_params={"names": field_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="custom_field",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.delete("/{field_id}")
async def delete_custom_field_api(
    field_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cf = await get_custom_field(db, field_id)
    cf_name = cf.field_name
    old_data = orm_to_dict(cf)
    prev_val, new_val, raw_val = await prepare_log_values(db, "custom_field", old_data, None)
    await delete_custom_field(db, field_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CUSTOM_FIELD_DELETE,
        operation_object_code="OBJ_CUSTOM_FIELD", operation_object_params={"name": cf_name},
        operation_content_code="LOG_CUSTOM_FIELD_DELETE", operation_content_params={"name": cf_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="custom_field",
        entity_id=field_id,
    )
    await db.commit()
    return {"success": True}


@router.get("/{field_id}/history")
async def get_custom_field_history(
    field_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from app.internal.cms_biz_system.services.operation_log_service import list_entity_history
    return await list_entity_history(db, "custom_field", field_id, limit)
