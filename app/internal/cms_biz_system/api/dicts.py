from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.user_crud import DictNodeCreate, DictNodeListItem, DictNodeUpdate, LanguageOption
from app.internal.cms_biz_system.services.dict_service import (
    create_node,
    delete_node,
    get_dict_children_by_code,
    get_multi_language_options,
    get_node,
    get_tree,
    toggle_status,
    update_node,
    _to_response,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict

router = APIRouter(prefix="/dicts")


@router.get("/multi-languages/options", response_model=list[LanguageOption])
async def get_multi_language_options_api(
    db: AsyncSession = Depends(get_db),
):
    return await get_multi_language_options(db)


@router.get("/", response_model=list[DictNodeListItem])
async def get_dict_tree(
    name: str | None = None,
    code: str | None = None,
    remark: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_tree(db, name, code, remark, sort_by, sort_order)


@router.get("/{code}/children", response_model=list[LanguageOption])
async def get_dict_children(
    code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """根据根节点 code 获取其所有活跃子节点（用于数据字典下拉选项）。"""
    return await get_dict_children_by_code(db, code)


@router.get("/{node_id}", response_model=DictNodeListItem)
async def get_dict_node_detail(
    node_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    node = await get_node(db, node_id)
    return _to_response(node)


@router.post("/", response_model=DictNodeListItem)
async def create_dict_node(
    body: DictNodeCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    node = await create_node(db, body)
    new_data = orm_to_dict(node)
    prev_val, new_val, raw_val = await prepare_log_values(db, "dict_node", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DICT_CREATE,
        operation_object_code="OBJ_DICT_NODE", operation_object_params={"name": node.name},
        operation_content_code="LOG_DICT_CREATE", operation_content_params={"name": node.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="dict_node",
        entity_id=node.id,
    )
    await db.commit()
    return node


@router.put("/{node_id}", response_model=DictNodeListItem)
async def update_dict_node(
    node_id: int,
    body: DictNodeUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_node = await get_node(db, node_id)
    old_data = orm_to_dict(old_node)
    node = await update_node(db, node_id, body)
    new_data = orm_to_dict(node)
    prev_val, new_val, raw_val = await prepare_log_values(db, "dict_node", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DICT_EDIT,
        operation_object_code="OBJ_DICT_NODE", operation_object_params={"name": node.name},
        operation_content_code="LOG_DICT_EDIT", operation_content_params={"name": node.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="dict_node",
        entity_id=node_id,
    )
    await db.commit()
    return node


@router.patch("/{node_id}/status")
async def update_dict_node_status(
    node_id: int,
    body: dict,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_node = await get_node(db, node_id)
    old_data = orm_to_dict(old_node)
    node = await toggle_status(db, node_id, body["status"])
    new_data = orm_to_dict(node)
    prev_val, new_val, raw_val = await prepare_log_values(db, "dict_node", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DICT_STATUS,
        operation_object_code="OBJ_DICT_NODE", operation_object_params={"name": node.name},
        operation_content_code="LOG_DICT_STATUS_ENABLED" if body["status"] == "active" else "LOG_DICT_STATUS_DISABLED",
        operation_content_params={"name": node.name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="dict_node",
        entity_id=node_id,
    )
    await db.commit()
    return {"id": node.id, "status": node.status}


@router.delete("/{node_id}")
async def delete_dict_node(
    node_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    node = await get_node(db, node_id)
    node_name = node.name
    old_data = orm_to_dict(node)
    await delete_node(db, node_id)
    prev_val, new_val, raw_val = await prepare_log_values(db, "dict_node", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.DICT_DELETE,
        operation_object_code="OBJ_DICT_NODE", operation_object_params={"name": node_name},
        operation_content_code="LOG_DICT_DELETE", operation_content_params={"name": node_name},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="dict_node",
        entity_id=node_id,
    )
    await db.commit()
    return {"success": True}
