"""
供应商（Provider）API 路由层。

路由前缀：/providers

接口列表：
    GET    /                           查询供应商列表（分页 + 过滤）
    POST   /                           新建供应商
    DELETE /batch                      批量软删除供应商
    GET    /simple                     获取所有供应商简要列表（下拉候选项）
    GET    /{provider_id}              查询单个供应商详情
    PUT    /{provider_id}              编辑供应商
    DELETE /{provider_id}              软删除供应商
    GET    /{provider_id}/contracts    查询供应商关联合同列表
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_scp.models.trade import Provider
from app.internal.cms_biz_scp.schemas.trade import (
    ContractListItem,
    ProviderCreate,
    ProviderHistoryItem,
    ProviderListItem,
    ProviderSimpleItem,
    ProviderUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_scp.services import provider_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log

router = APIRouter(prefix="/providers")


@router.get("/", response_model=PaginatedResponse[ProviderListItem])
async def get_provider_list(
    page: int = 1,
    page_size: int = 10,
    provider_code: str | None = None,
    name: str | None = None,
    country: str | None = None,
    notes: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await provider_service.list_providers(db, page, page_size, provider_code, name, country, notes, sort_by, sort_order)


@router.post("/", response_model=ProviderListItem)
async def create_provider_api(
    body: ProviderCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await provider_service.create_provider(db, body)
    new_data = orm_to_dict(result)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "provider", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PROVIDER_CREATE,
        operation_object_code="OBJ_PROVIDER", operation_object_params={"name": body.name},
        operation_content_code="LOG_PROVIDER_CREATE", operation_content_params={"name": body.name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="provider",
        entity_id=result.id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/batch")
async def batch_delete_providers_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    providers = (await db.execute(select(Provider).where(Provider.id.in_(body.ids), Provider.is_deleted.is_(False)))).scalars().all()
    provider_names = ", ".join([p.name for p in providers]) if providers else str(body.ids)
    prev_data = [orm_to_dict(p) for p in providers]
    prev_val, _, raw_val = await prepare_log_values(db, "provider", prev_data, None)
    deleted = await provider_service.batch_delete_providers(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PROVIDER_BATCH_DELETE,
        operation_object_code="OBJ_PROVIDER", operation_object_params={"name": provider_names},
        operation_content_code="LOG_PROVIDER_BATCH_DELETE", operation_content_params={"names": provider_names},
        entity_type="provider",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.get("/simple", response_model=list[ProviderSimpleItem])
async def get_providers_simple(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await provider_service.get_all_providers_simple(db)


@router.get("/{provider_id}", response_model=ProviderListItem)
async def get_provider_detail(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await provider_service.get_provider(db, provider_id)


@router.put("/{provider_id}", response_model=ProviderListItem)
async def update_provider_api(
    provider_id: int,
    body: ProviderUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await provider_service.get_provider(db, provider_id)
    result = await provider_service.update_provider(db, provider_id, body)
    old_data = orm_to_dict(old)
    new_data = orm_to_dict(result)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "provider", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PROVIDER_EDIT,
        operation_object_code="OBJ_PROVIDER", operation_object_params={"name": old.name},
        operation_content_code="LOG_PROVIDER_EDIT", operation_content_params={"name": old.name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="provider",
        entity_id=provider_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/{provider_id}")
async def delete_provider_api(
    provider_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    provider = await provider_service.get_provider(db, provider_id)
    provider_name = provider.name
    old_data = orm_to_dict(provider)
    await provider_service.delete_provider(db, provider_id)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "provider", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PROVIDER_DELETE,
        operation_object_code="OBJ_PROVIDER", operation_object_params={"name": provider_name},
        operation_content_code="LOG_PROVIDER_DELETE", operation_content_params={"name": provider_name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="provider",
        entity_id=provider_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/{provider_id}/contracts", response_model=list[ContractListItem])
async def get_provider_contracts(
    provider_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await provider_service.list_provider_contracts(db, provider_id)


@router.get("/{provider_id}/history", response_model=list[ProviderHistoryItem])
async def get_provider_history(
    provider_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询供应商操作历史（Processed History）。"""
    return await provider_service.list_provider_history(db, provider_id, limit)
