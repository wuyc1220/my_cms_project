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
from app.common.core.i18n import get_msg
from app.soap.c2 import CategorySyncBuilder
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
        operation_object_code="OBJ_CATEGORY", operation_object_params={"name": body.name},
        operation_content_code="LOG_CATEGORY_CREATE", operation_content_params={"name": body.name},
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
    categories = (await db.execute(select(Category).where(Category.id.in_(body.ids)))).scalars().all()
    cat_names = ", ".join([c.name for c in categories]) if categories else str(body.ids)
    prev_data = [orm_to_dict(c) for c in categories]
    prev_val, _, raw_val = await prepare_log_values(db, "category", prev_data, None)
    deleted = await batch_delete_categories(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CATEGORY_BATCH_DELETE,
        operation_object_code="OBJ_CATEGORY", operation_object_params={"name": cat_names},
        operation_content_code="LOG_CATEGORY_BATCH_DELETE", operation_content_params={"names": cat_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="category",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
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
        operation_object_code="OBJ_CATEGORY", operation_object_params={"name": old.name},
        operation_content_code="LOG_CATEGORY_EDIT", operation_content_params={"name": cat.name},
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
        operation_object_code="OBJ_CATEGORY", operation_object_params={"name": cat_name},
        operation_content_code="LOG_CATEGORY_DELETE", operation_content_params={"name": cat_name},
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    import json
    # 先查询栏目信息（用于日志显示），再执行移除
    category = await get_category(db, category_id)
    category_name = category.name if category else None

    await remove_category_content(db, category_id, content_id)

    # 已发布内容取消关联后回滚状态
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
    from app.internal.cms_biz_orchestration.services.workflow_service import (
        rollback_after_published_edit,
        complete_process_and_update_status,
    )
    content = await get_content_by_id(db, content_id)
    if content:
        await rollback_after_published_edit(
            db, content_id, content.content_type,
            current_user.username, "取消栏目关联",
        )

    # 记录 Category 流程节点（与关联栏目对称，状态回滚已由 rollback_after_published_edit 处理）
    if content and category_name:
        await complete_process_and_update_status(
            db,
            content_id=content_id,
            content_type=content.content_type,
            process_name="Category",
            processed_by=current_user.username,
            info=f"取消栏目关联: {category_name}",
            skip_status_update=True,
        )

    # 写入内容详情页活动日志（与内容详情页入口 live.py#unlink_content_category_api 保持一致）
    prev_val = json.dumps({"category_name": category_name}, ensure_ascii=False) if category_name else None
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_CATEGORY_UNLINK,
        operation_object_code="log.category.unlink",
        operation_content_code="log.category.unlink",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=prev_val,
        updated_value=None,
        # 原始入参快照：被解除关联的栏目 ID
        updated_value_json=json.dumps({"content_id": content_id, "category_id": category_id}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
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


# ---------- Sync to Business System ----------

class CategorySyncRequest(BaseModel):
    """Category 同步请求体"""
    category_ids: list[int]

class CategorySyncResponse(BaseModel):
    """Category 同步响应"""
    success: bool
    file_path: str
    stats: dict
    synced_ids: list[int]
    message: str
    correlate_id: str | None = None
    soap_success: bool | None = None


@router.post("/sync", response_model=CategorySyncResponse)
async def sync_categories_to_business(
    body: CategorySyncRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    将选中的 Category（栏目）同步给业务系统。

    生成符合 C2 规范的 ADI XML，包含：
    - Category 对象（REGIST/UPDATE/SKIP 基于变更检测）
    - 关联的 Picture 对象
    - Picture → Category 的 Mapping 关系

    同步策略：
    - 首次同步 → REGIST
    - 已同步且无变更 → SKIP（不输出 Object，不影响已分发数据）
    - 已同步且有变更 → UPDATE
    """
    category_ids = body.category_ids
    if not category_ids:
        return CategorySyncResponse(
            success=False,
            file_path="",
            stats={"regist": 0, "update": 0, "skip": 0},
            synced_ids=[],
            message=get_msg("CATEGORY_IDS_REQUIRED"),
        )

    try:
        builder = CategorySyncBuilder(db)
        result = await builder.build_sync(category_ids)

        # 记录操作日志
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type="CATEGORY_SYNC",
            operation_object_code="OBJ_CATEGORY",
            operation_content_code="LOG_CATEGORY_SYNC",
            operation_content_params={"result": f"ids={category_ids}, stats={result.get('stats', '')}, CorrelateID={result.get('correlate_id', '')}"},
            ip_address=_get_ip(request),
            result="success",
            entity_type="category",
            entity_id=category_ids[0],
        )
        await db.commit()

        stats = result["stats"]
        soap_success = result.get("soap_success", True)
        correlate_id = result.get("correlate_id", "")

        if not correlate_id:
            # 全部 SKIP，未发送 SOAP 通知
            message = get_msg("CATEGORY_SYNC_NO_CHANGE")
        elif soap_success:
            message = get_msg("CATEGORY_SYNC_SUBMITTED", correlate_id=correlate_id)
        else:
            message = get_msg("CATEGORY_SYNC_SOAP_FAILED", error=result.get('soap_error', get_msg("CATEGORY_SYNC_UNKNOWN_ERROR")))

        return CategorySyncResponse(
            success=soap_success,
            file_path=result["file_path"],
            stats=stats,
            synced_ids=result["synced_ids"],
            message=message,
            correlate_id=correlate_id,
            soap_success=soap_success,
        )

    except Exception as e:
        logger = __import__("loguru").logger
        logger.error(f"[CategorySync] 同步失败: {e}")
        await db.rollback()
        return CategorySyncResponse(
            success=False,
            file_path="",
            stats={"regist": 0, "update": 0, "skip": 0},
            synced_ids=[],
            message=get_msg("CATEGORY_SYNC_FAILED", error=str(e)),
        )
