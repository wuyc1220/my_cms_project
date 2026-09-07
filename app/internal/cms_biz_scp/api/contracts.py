"""
合同（Contract）API 路由层。

路由前缀：/contracts

接口列表：
    GET    /                                        查询合同列表（分页 + 过滤）
    POST   /                                        新建合同
    DELETE /batch                                   批量软删除合同
    GET    /simple                                  获取所有合同简要列表（下拉候选项）
    GET    /without-license-count                   统计无许可证合同数量
    GET    /{contract_id}                           查询单个合同详情
    PUT    /{contract_id}                           编辑合同
    DELETE /{contract_id}                           软删除合同
    GET    /{contract_id}/licenses                  查询合同关联许可证列表（简要，供弹框中栏）
    GET    /{contract_id}/attachments               查询合同附件列表
    DELETE /{contract_id}/attachments/{id}          删除合同附件
    
    注：附件上传/下载使用通用接口 /attachments/upload 和 /attachments/download
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_scp.models.trade import Contract
from app.internal.cms_biz_scp.schemas.trade import (
    ContractAttachmentItem,
    ContractCreate,
    ContractHistoryItem,
    ContractListItem,
    ContractSimpleItem,
    ContractUpdate,
    LicenseSimpleItem,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_scp.services import contract_service, license_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log

from app.internal.cms_biz_orchestration.services.storage import storage_service

router = APIRouter(prefix="/contracts")


@router.get("/", response_model=PaginatedResponse[ContractListItem])
async def get_contract_list(
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    provider_id: int | None = None,
    platforms: list[str] | None = Query(default=None),
    start_date_from: str | None = None,
    start_date_to: str | None = None,
    end_date_from: str | None = None,
    end_date_to: str | None = None,
    without_license: bool = False,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await contract_service.list_contracts(
        db, page, page_size, name, provider_id, platforms,
        start_date_from, start_date_to,
        end_date_from, end_date_to,
        without_license, sort_by, sort_order,
    )


@router.post("/", response_model=ContractListItem)
async def create_contract_api(
    body: ContractCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await contract_service.create_contract(db, body)
    new_data = orm_to_dict(result)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "contract", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_CREATE,
        operation_object_code="OBJ_CONTRACT", operation_object_params={"name": body.name},
        operation_content_code="LOG_CONTRACT_CREATE", operation_content_params={"name": body.name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="contract",
        entity_id=result.id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/batch")
async def batch_delete_contracts_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contracts = (await db.execute(select(Contract).where(Contract.id.in_(body.ids), Contract.is_deleted.is_(False)))).scalars().all()
    contract_names = ", ".join([c.name for c in contracts]) if contracts else str(body.ids)
    prev_data = [orm_to_dict(c) for c in contracts]
    prev_val, _, raw_val = await prepare_log_values(db, "contract", prev_data, None)
    deleted = await contract_service.batch_delete_contracts(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_BATCH_DELETE,
        operation_object_code="OBJ_CONTRACT", operation_object_params={"name": contract_names},
        operation_content_code="LOG_CONTRACT_BATCH_DELETE", operation_content_params={"names": contract_names},
        entity_type="contract",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.get("/simple", response_model=list[ContractSimpleItem])
async def get_contracts_simple(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await contract_service.get_all_contracts_simple(db)


@router.get("/without-license-count")
async def get_without_license_count(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    count = await contract_service.get_without_license_count(db)
    return {"count": count}


@router.get("/{contract_id}", response_model=ContractListItem)
async def get_contract_detail(
    contract_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await contract_service.get_contract(db, contract_id)


@router.put("/{contract_id}", response_model=ContractListItem)
async def update_contract_api(
    contract_id: int,
    body: ContractUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await contract_service.get_contract(db, contract_id)
    result = await contract_service.update_contract(db, contract_id, body)
    old_data = orm_to_dict(old)
    new_data = orm_to_dict(result)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "contract", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_EDIT,
        operation_object_code="OBJ_CONTRACT", operation_object_params={"name": old.name},
        operation_content_code="LOG_CONTRACT_EDIT", operation_content_params={"name": old.name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="contract",
        entity_id=contract_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.delete("/{contract_id}")
async def delete_contract_api(
    contract_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    contract = await contract_service.get_contract(db, contract_id)
    contract_name = contract.name
    old_data = orm_to_dict(contract)
    await contract_service.delete_contract(db, contract_id)
    previous_value, updated_value, updated_value_json = await prepare_log_values(db, "contract", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_DELETE,
        operation_object_code="OBJ_CONTRACT", operation_object_params={"name": contract_name},
        operation_content_code="LOG_CONTRACT_DELETE", operation_content_params={"name": contract_name},
        previous_value=previous_value,
        updated_value=updated_value,
        updated_value_json=updated_value_json,
        entity_type="contract",
        entity_id=contract_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/{contract_id}/licenses", response_model=list[LicenseSimpleItem])
async def get_contract_licenses(
    contract_id: int,
    name: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await license_service.get_contract_licenses_simple(db, contract_id, name)


@router.get("/{contract_id}/history", response_model=list[ContractHistoryItem])
async def get_contract_history(
    contract_id: int,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询合同操作历史（Processed History）。"""
    return await contract_service.list_contract_history(db, contract_id, limit)


@router.get("/{contract_id}/attachments", response_model=list[ContractAttachmentItem])
async def get_contract_attachments(
    contract_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    attachments = await contract_service.get_contract_attachments(db, contract_id)
    return [
        ContractAttachmentItem(
            id=a.id,
            contract_id=a.contract_id,
            file_name=a.file_name,
            file_path=a.file_path,
            relative_path=a.relative_path,
            file_size=a.file_size,
            uploaded_by=a.uploaded_by,
            created_at=a.created_at,
            url=storage_service.get_file_url(a.file_path, a.relative_path),
        )
        for a in attachments
    ]


@router.post("/{contract_id}/attachments/register", response_model=ContractAttachmentItem)
async def register_attachment_api(
    contract_id: int,
    request: Request,
    file_path: str,
    file_name: str,
    file_size: int,
    storage_url: str = Query(default="", description="加密的完整存储URL"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    注册合同附件记录(文件已通过通用上传接口 /attachments/upload?category=contracts 上传)。
    
    前端流程:
    1. 调用 POST /attachments/upload?category=contracts 上传文件
    2. 获取返回的 file_path(相对路径), storage_url(加密全路径), file_name, file_size
    3. 调用此接口将附件信息注册到合同附件表中,同时存储两个路径:
       - file_path: 加密全路径(存储用、C2规范用)
       - relative_path: 相对路径(下载用)
    """
    # 优先使用 storage_url(加密全路径),如果没有则使用 file_path 转换
    if storage_url:
        encrypted_file_path = storage_url
    else:
        # 兼容旧调用:如果没有传 storage_url,则从 file_path 转换
        encrypted_file_path = storage_service.get_storage_url(file_path)
    
    # file_path 参数就是相对路径
    attachment = await contract_service.upload_attachment(
        db, contract_id, encrypted_file_path, file_name, file_size, current_user.id,
        relative_path=file_path,
    )
    import json as _json
    upd_val = _json.dumps({"file_name": file_name, "file_size": file_size}, ensure_ascii=False)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_ATTACHMENT_UPLOAD,
        operation_object_code="OBJ_CONTRACT_ATTACHMENT", operation_object_params={"name": file_name},
        operation_content_code="LOG_CONTRACT_ATTACHMENT_UPLOAD", operation_content_params={"name": file_name},
        updated_value=upd_val,
        # 原始入参快照（上传的文件信息 + 两条存储路径）
        updated_value_json=_json.dumps({
            "file_name": file_name,
            "file_size": file_size,
            "file_path": encrypted_file_path,
            "relative_path": file_path,
        }, ensure_ascii=False),
        entity_type="contract",
        entity_id=contract_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return ContractAttachmentItem(
        id=attachment.id,
        contract_id=attachment.contract_id,
        file_name=attachment.file_name,
        file_path=attachment.file_path,
        relative_path=attachment.relative_path,
        file_size=attachment.file_size,
        uploaded_by=attachment.uploaded_by,
        created_at=attachment.created_at,
        url=storage_service.get_file_url(attachment.file_path, attachment.relative_path),
    )





@router.delete("/{contract_id}/attachments/{attachment_id}")
async def delete_attachment_api(
    contract_id: int,
    attachment_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    attachment = await contract_service.get_attachment(db, contract_id, attachment_id)
    file_name = attachment.file_name
    file_size = attachment.file_size
    await contract_service.delete_attachment(db, contract_id, attachment_id)
    import json as _json
    prev_val = _json.dumps({"file_name": file_name, "file_size": file_size}, ensure_ascii=False)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_ATTACHMENT_DELETE,
        operation_object_code="OBJ_CONTRACT_ATTACHMENT", operation_object_params={"name": file_name},
        operation_content_code="LOG_CONTRACT_ATTACHMENT_DELETE", operation_content_params={"name": file_name},
        entity_type="contract",
        entity_id=contract_id,
        previous_value=prev_val,
        # 删除语义：原始快照存被删附件信息（与 previous_value 同源，未富化）
        updated_value_json=prev_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}
