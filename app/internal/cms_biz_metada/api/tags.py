from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_metada.models.basic import Tag
from app.internal.cms_biz_metada.schemas.basic import TagCreate, TagListItem, TagUpdate
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.internal.cms_biz_metada.services.tag_service import batch_delete_tags, create_tag, delete_tag, get_tag, list_tags, update_tag
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values

router = APIRouter(prefix="/tags")


@router.get("/", response_model=PaginatedResponse[TagListItem])
async def get_tag_list(
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
    logger.info(f"[Tag] 查询参数: page={page}, page_size={page_size}, name={name}, languages={languages}, sort_by={sort_by}, sort_order={sort_order}")
    return await list_tags(db, page, page_size, name, languages, sort_by, sort_order)


@router.get("/{tag_id}", response_model=TagListItem)
async def get_tag_detail(
    tag_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return TagListItem.model_validate(await get_tag(db, tag_id))


@router.post("/", response_model=TagListItem)
async def create_tag_api(
    body: TagCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tag = await create_tag(db, body)
    new_data = orm_to_dict(tag)
    prev_val, new_val, raw_val = await prepare_log_values(db, "tag", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.TAG_CREATE,
        operation_object=f"标签 {body.name}",
        operation_content=f"Created tag: name={body.name}, language={body.language}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="tag",
        entity_id=tag.id,
    )
    await db.commit()
    return TagListItem.model_validate(tag)


@router.put("/{tag_id}", response_model=TagListItem)
async def update_tag_api(
    tag_id: int,
    body: TagUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_tag(db, tag_id)
    old_data = orm_to_dict(old)
    old_name = old.name
    tag = await update_tag(db, tag_id, body)
    new_data = orm_to_dict(tag)
    prev_val, new_val, raw_val = await prepare_log_values(db, "tag", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.TAG_EDIT,
        operation_object=f"标签 {old_name}",
        operation_content=f"Updated tag: ID={tag_id}, name={tag.name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="tag",
        entity_id=tag_id,
    )
    await db.commit()
    return TagListItem.model_validate(tag)


@router.delete("/batch")
async def batch_delete_tags_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Tag.name).where(Tag.id.in_(body.ids)))).scalars().all()
    tag_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await batch_delete_tags(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.TAG_BATCH_DELETE,
        operation_object=f"标签 {tag_names}",
        operation_content=f"批量删除标签: {tag_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="tag",
        entity_id=body.ids[0] if body.ids else None,
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.delete("/{tag_id}")
async def delete_tag_api(
    tag_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    tag = await get_tag(db, tag_id)
    tag_name = tag.name
    old_data = orm_to_dict(tag)
    prev_val, new_val, raw_val = await prepare_log_values(db, "tag", old_data, None)
    await delete_tag(db, tag_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.TAG_DELETE,
        operation_object=f"标签 {tag_name}",
        operation_content=f"Deleted tag: ID={tag_id}, name={tag_name}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="tag",
        entity_id=tag_id,
    )
    await db.commit()
    return {"success": True}
