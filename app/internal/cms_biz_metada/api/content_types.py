from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_metada.models.basic import ContentType
from app.internal.cms_biz_metada.schemas.basic import ContentTypeCreate, ContentTypeListItem, ContentTypeUpdate
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.internal.cms_biz_metada.services.content_type_service import (
    batch_delete_content_types,
    create_content_type,
    delete_content_type,
    get_content_type,
    list_content_types,
    update_content_type,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values

router = APIRouter(prefix="/content-types")


@router.get("/", response_model=PaginatedResponse[ContentTypeListItem])
async def get_content_type_list(
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_content_types(db, page, page_size, name, sort_by, sort_order)


@router.get("/{type_id}", response_model=ContentTypeListItem)
async def get_content_type_detail(
    type_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return ContentTypeListItem.model_validate(await get_content_type(db, type_id))


@router.post("/", response_model=ContentTypeListItem)
async def create_content_type_api(
    body: ContentTypeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ct = await create_content_type(db, body)
    new_data = orm_to_dict(ct)
    prev_val, new_val, raw_val = await prepare_log_values(db, "content_type", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_TYPE_CREATE,
        operation_object=f"内容类型 {body.name}",
        operation_content=f"Created content type: name={body.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="content_type",
        entity_id=ct.id,
    )
    await db.commit()
    return ContentTypeListItem.model_validate(ct)


@router.put("/{type_id}", response_model=ContentTypeListItem)
async def update_content_type_api(
    type_id: int,
    body: ContentTypeUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_content_type(db, type_id)
    old_data = orm_to_dict(old)
    old_name = old.name
    ct = await update_content_type(db, type_id, body)
    new_data = orm_to_dict(ct)
    prev_val, new_val, raw_val = await prepare_log_values(db, "content_type", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_TYPE_EDIT,
        operation_object=f"内容类型 {old_name}",
        operation_content=f"Updated content type: ID={type_id}, name={ct.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="content_type",
        entity_id=type_id,
    )
    await db.commit()
    return ContentTypeListItem.model_validate(ct)


@router.delete("/batch")
async def batch_delete_content_types_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(ContentType.name).where(ContentType.id.in_(body.ids)))).scalars().all()
    ct_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await batch_delete_content_types(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_TYPE_BATCH_DELETE,
        operation_object=f"内容类型 {ct_names}",
        operation_content=f"批量删除内容类型: {ct_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="content_type",
        entity_id=body.ids[0] if body.ids else None,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.delete("/{type_id}")
async def delete_content_type_api(
    type_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ct = await get_content_type(db, type_id)
    ct_name = ct.name
    old_data = orm_to_dict(ct)
    prev_val, new_val, raw_val = await prepare_log_values(db, "content_type", old_data, None)
    await delete_content_type(db, type_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_TYPE_DELETE,
        operation_object=f"内容类型 {ct_name}",
        operation_content=f"Deleted content type: ID={type_id}, name={ct_name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="content_type",
        entity_id=type_id,
    )
    await db.commit()
    return {"success": True}
