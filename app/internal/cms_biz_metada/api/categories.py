from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.schemas import BatchDeleteRequest
from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_metada.models.basic import Category
from app.internal.cms_biz_metada.schemas.basic import (
    CategoryCreate, CategoryListItem, CategoryUpdate,
    EntityFieldValueItem, EntityFieldValuesPayload,
    EntityI18nItem, EntityI18nPayload,
)
from app.internal.cms_biz_metada.services.category_service import (
    batch_delete_categories,
    create_category,
    delete_category,
    get_category,
    get_category_tree,
    update_category,
    get_category_contents,
    reorder_category_contents,
    remove_category_content,
)
from app.internal.cms_biz_metada.services.entity_data_service import (
    get_field_values,
    save_field_values,
    get_i18n_values,
    save_i18n_values,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from pydantic import BaseModel

router = APIRouter(prefix="/categories")

ENTITY_TYPE = "category"


def _to_list_item(cat) -> CategoryListItem:
    return CategoryListItem(
        id=cat.id,
        parent_id=cat.parent_id,
        platform=cat.platform,
        name=cat.name,
        sequence=cat.sequence,
        category_type=cat.category_type,
        vod_count=cat.vod_count,
        description=cat.description,
        jump_category_code=cat.jump_category_code,
        status=cat.status,
        ingest_status=cat.ingest_status,
        children=[],
        created_at=cat.created_at,
    )


@router.get("/", response_model=list[CategoryListItem])
async def get_category_tree_api(
    name: str | None = None,
    platforms: list[str] | None = Query(default=None),
    category_types: list[str] | None = Query(default=None),
    ingest_statuses: list[str] | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_category_tree(db, name, platforms, category_types, ingest_statuses)


@router.get("/{category_id}", response_model=CategoryListItem)
async def get_category_detail(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return _to_list_item(await get_category(db, category_id))


@router.post("/", response_model=CategoryListItem)
async def create_category_api(
    body: CategoryCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cat = await create_category(db, body)
    new_data = orm_to_dict(cat)
    prev_val, new_val, raw_val = await prepare_log_values(db, "category", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CATEGORY_CREATE,
        operation_object=f"栏目 {body.name}",
        operation_content=f"Created category: name={body.name}, platform={body.platform}, type={body.category_type}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="category",
        entity_id=cat.id,
    )
    await db.commit()
    return _to_list_item(cat)


@router.delete("/batch")
async def batch_delete_categories_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Category.name).where(Category.id.in_(body.ids)))).scalars().all()
    cat_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await batch_delete_categories(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CATEGORY_BATCH_DELETE,
        operation_object=f"栏目 {cat_names}",
        operation_content=f"批量删除栏目: {cat_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="category",
        entity_id=body.ids[0] if body.ids else None,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.put("/{category_id}", response_model=CategoryListItem)
async def update_category_api(
    category_id: int,
    body: CategoryUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_category(db, category_id)
    old_data = orm_to_dict(old)
    cat = await update_category(db, category_id, body)
    new_data = orm_to_dict(cat)
    prev_val, new_val, raw_val = await prepare_log_values(db, "category", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CATEGORY_EDIT,
        operation_object=f"栏目 {old.name}",
        operation_content=f"Updated category: ID={category_id}, name={cat.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="category",
        entity_id=category_id,
    )
    await db.commit()
    return _to_list_item(cat)


@router.delete("/{category_id}")
async def delete_category_api(
    category_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    cat = await get_category(db, category_id)
    cat_name = cat.name
    old_data = orm_to_dict(cat)
    prev_val, new_val, raw_val = await prepare_log_values(db, "category", old_data, None)
    await delete_category(db, category_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CATEGORY_DELETE,
        operation_object=f"栏目 {cat_name}",
        operation_content=f"Deleted category: ID={category_id}, name={cat_name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="category",
        entity_id=category_id,
    )
    await db.commit()
    return {"success": True}


# ---------- Custom Field Values ----------

@router.get("/{category_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_category_field_values(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_field_values(db, ENTITY_TYPE, category_id)


@router.put("/{category_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_category_field_values(
    category_id: int,
    body: EntityFieldValuesPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await save_field_values(db, ENTITY_TYPE, category_id, body)


# ---------- Multi-Language Values ----------

@router.get("/{category_id}/i18n", response_model=list[EntityI18nItem])
async def get_category_i18n(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_i18n_values(db, ENTITY_TYPE, category_id)


@router.put("/{category_id}/i18n", response_model=list[EntityI18nItem])
async def save_category_i18n(
    category_id: int,
    body: EntityI18nPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await save_i18n_values(db, ENTITY_TYPE, category_id, body)


# ---------- Category Contents ----------

class ContentOrderPayload(BaseModel):
    content_ids: list[int]


@router.get("/{category_id}/contents")
async def get_category_contents_api(
    category_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_category_contents(db, category_id)


@router.put("/{category_id}/contents/order")
async def reorder_category_contents_api(
    category_id: int,
    body: ContentOrderPayload,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await reorder_category_contents(db, category_id, body.content_ids)
    return {"success": True}


@router.delete("/{category_id}/contents/{content_id}")
async def remove_category_content_api(
    category_id: int,
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    await remove_category_content(db, category_id, content_id)
    return {"success": True}


@router.get("/{category_id}/history")
async def get_category_history(
    category_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    from app.internal.cms_biz_system.services.operation_log_service import list_entity_history
    return await list_entity_history(db, "category", category_id, limit)
