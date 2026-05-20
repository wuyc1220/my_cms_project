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
    POST   /{contract_id}/attachments               上传合同附件（multipart/form-data）
    GET    /{contract_id}/attachments/{id}/download 下载合同附件
    DELETE /{contract_id}/attachments/{id}          删除合同附件
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import Response
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
        operation_object=f"CONTRACT {body.name}",
        operation_content="LOG_CONTRACT_CREATED",
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
    rows = (await db.execute(select(Contract.name).where(Contract.id.in_(body.ids), Contract.is_deleted.is_(False)))).scalars().all()
    contract_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await contract_service.batch_delete_contracts(db, body)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_BATCH_DELETE,
        operation_object=f"合同 {contract_names}",
        operation_content=f"批量删除合同: {contract_names}",
        entity_type="contract",
        entity_id=body.ids[0] if body.ids else None,
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
        operation_object=f"CONTRACT {old.name}",
        operation_content="LOG_CONTRACT_UPDATED",
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
        operation_object=f"CONTRACT {contract_name}",
        operation_content="LOG_CONTRACT_DELETED",
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
            file_size=a.file_size,
            uploaded_by=a.uploaded_by,
            created_at=a.created_at,
        )
        for a in attachments
    ]


@router.post("/{contract_id}/attachments", response_model=ContractAttachmentItem)
async def upload_attachment_api(
    contract_id: int,
    request: Request,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    file_content = await file.read()
    try:
        attachment = await contract_service.upload_attachment(
            db, contract_id, file_content, file.filename or "unnamed", current_user.id
        )
    except Exception as e:
        raise BusinessException(ErrorCode.INTERNAL_ERROR, f"Failed to upload file: {e}")
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_ATTACHMENT_UPLOAD,
        operation_object=f"CONTRACT_ATTACHMENT {file.filename}",
        operation_content="LOG_CONTRACT_ATTACHMENT_UPLOADED",
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
        file_size=attachment.file_size,
        uploaded_by=attachment.uploaded_by,
        created_at=attachment.created_at,
    )


@router.get("/{contract_id}/attachments/{attachment_id}/download")
async def download_attachment_api(
    contract_id: int,
    attachment_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    attachment = await contract_service.get_attachment(db, contract_id, attachment_id)
    try:
        file_content = storage_service.get_file(attachment.file_path)
    except FileNotFoundError:
        raise NotFoundException(ErrorCode.NOT_FOUND, "File not found on storage")
    except Exception as e:
        raise BusinessException(ErrorCode.INTERNAL_ERROR, f"Failed to read file: {e}")
    encoded_name = quote(attachment.file_name, safe="")
    try:
        ascii_name = attachment.file_name.encode("ascii").decode("ascii")
        disposition = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    except (UnicodeEncodeError, UnicodeDecodeError):
        disposition = f"attachment; filename*=UTF-8''{encoded_name}"
    return Response(
        content=file_content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": disposition},
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
    await contract_service.delete_attachment(db, contract_id, attachment_id)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTRACT_ATTACHMENT_DELETE,
        operation_object=f"CONTRACT_ATTACHMENT {file_name}",
        operation_content="LOG_CONTRACT_ATTACHMENT_DELETED",
        entity_type="contract",
        entity_id=contract_id,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}
