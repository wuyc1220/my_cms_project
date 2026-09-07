"""
合同（Contract）业务逻辑层。

职责：
- Contract 的 CRUD（新增、查询、编辑、软删除、批量软删除）
- 合同平台关系维护（ContractPlatform）
- 合同附件管理（ContractAttachment）
- 获取合同简要列表（用于许可证表单下拉）
- 合同关联许可证列表查询

业务规则：
1. 合同名称全局唯一，重复时抛出 400 错误
2. 更新平台时，先删除旧记录再批量插入新记录
3. 软删除：is_deleted=True，不物理删除
4. 若存在未删除的许可证，则拒绝删除合同
"""

from datetime import date as date_type

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException, NotFoundException, ErrorCode

from ..models.trade import Contract, ContractAttachment, ContractPlatform, License, LicenseContent
from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.internal.cms_biz_scp.schemas.trade import (
    ContractCreate,
    ContractHistoryItem,
    ContractListItem,
    ContractPlatformItem,
    ContractSimpleItem,
    ContractUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from loguru import logger
from app.common.core.i18n import get_msg


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _build_contract_item(db: AsyncSession, contract: Contract) -> ContractListItem:
    """将 ORM 对象转换为响应 schema，附加许可证数量。"""
    license_count = (
        await db.execute(
            select(func.count(License.id)).where(
                License.contract_id == contract.id,
                License.is_deleted.is_(False),
            )
        )
    ).scalar_one()

    return ContractListItem(
        id=contract.id,
        name=contract.name,
        provider_id=contract.provider_id,
        provider_name=contract.provider.name if contract.provider else "",
        platforms=[
            ContractPlatformItem(platform=p.platform, commercial_rights=p.commercial_rights)
            for p in contract.platforms
        ],
        start_date=contract.start_date,
        end_date=contract.end_date,
        notes=contract.notes,
        license_count=license_count,
        created_at=contract.created_at,
    )


async def _get_contract_or_404(db: AsyncSession, contract_id: int) -> Contract:
    """查询合同，不存在则抛 404。"""
    result = await db.execute(
        select(Contract).where(Contract.id == contract_id, Contract.is_deleted.is_(False))
    )
    contract = result.scalar_one_or_none()
    if not contract:
        raise NotFoundException(ErrorCode.CONTRACT_NOT_FOUND, get_msg("CONTRACT_NOT_FOUND"))
    return contract


async def _check_name_unique(db: AsyncSession, name: str, exclude_id: int | None = None) -> None:
    """校验合同名称唯一性。"""

    query = select(Contract.id).where(Contract.name == name, Contract.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(Contract.id != exclude_id)
    # 使用 limit(1) 避免数据库中存在多条同名记录时报错
    if (await db.execute(query.limit(1))).scalar_one_or_none():
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTRACT_NAME_EXISTS", name=name))


async def _update_platforms(
    db: AsyncSession, contract_id: int, platforms: list[ContractPlatformItem]
) -> None:
    """删除旧平台记录后批量插入新记录。"""
    old = (
        await db.execute(
            select(ContractPlatform).where(ContractPlatform.contract_id == contract_id)
        )
    ).scalars().all()
    for old_p in old:
        await db.delete(old_p)
    await db.flush()
    for p in platforms:
        db.add(ContractPlatform(
            contract_id=contract_id,
            platform=p.platform,
            commercial_rights=p.commercial_rights,
        ))


# ─── Contract CRUD ────────────────────────────────────────────────────

async def list_contracts(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    provider_id: int | None = None,
    platforms: list[str] | None = None,
    start_date_from: str | None = None,
    start_date_to: str | None = None,
    end_date_from: str | None = None,
    end_date_to: str | None = None,
    without_license: bool = False,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[ContractListItem]:
    """
    查询合同列表（分页）。

    输入参数：
        page                页码（从 1 开始）
        page_size           每页条数
        name                合同名称关键字（模糊匹配）
        provider_id         供应商 id（精确匹配）
        platforms           平台列表（任一匹配）
        start_date_from     开始日期范围下限（YYYY-MM-DD）
        start_date_to       开始日期范围上限
        end_date_from       结束日期范围下限
        end_date_to         结束日期范围上限
        without_license     仅返回无关联许可证的合同

    输出：
        PaginatedResponse[ContractListItem]
    """
    logger.info(f"list_contracts 入参: page={page}, page_size={page_size}, name={name}, provider_id={provider_id}, platforms={platforms}, start_date_from={start_date_from}, start_date_to={start_date_to}, end_date_from={end_date_from}, end_date_to={end_date_to}, without_license={without_license}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Contract).where(Contract.is_deleted.is_(False))

    if name:
        query = query.where(Contract.name.ilike(f"%{name}%"))
    if provider_id is not None:
        query = query.where(Contract.provider_id == provider_id)
    if platforms:
        from sqlalchemy import exists
        sub = select(ContractPlatform.contract_id).where(
            ContractPlatform.platform.in_(platforms)
        )
        query = query.where(Contract.id.in_(sub))
    if start_date_from:
        query = query.where(Contract.start_date >= date_type.fromisoformat(start_date_from))
    if start_date_to:
        query = query.where(Contract.start_date <= date_type.fromisoformat(start_date_to))
    if end_date_from:
        query = query.where(Contract.end_date >= date_type.fromisoformat(end_date_from))
    if end_date_to:
        query = query.where(Contract.end_date <= date_type.fromisoformat(end_date_to))
    if without_license:
        sub_license_exists = select(License).where(
            License.contract_id == Contract.id,
            License.is_deleted.is_(False),
        )
        query = query.where(~sub_license_exists.exists())

    if sort_by and sort_order:
        if sort_by == "provider_name":
            from ..models.trade import Provider
            query = query.outerjoin(Provider, Provider.id == Contract.provider_id)
            sort_column = Provider.name
        elif sort_by == "license_count":
            license_count_subq = (
                select(func.count(License.id).label("license_count"))
                .where(
                    License.contract_id == Contract.id,
                    License.is_deleted.is_(False),
                )
                .correlate(Contract)
                .scalar_subquery()
            )
            query = query.add_columns(license_count_subq.label("license_count"))
            sort_column = license_count_subq
        else:
            sort_column = getattr(Contract, sort_by, None)
        
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, Contract.id.desc())
        else:
            query = query.order_by(Contract.id.desc())
    else:
        query = query.order_by(Contract.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    if sort_by == "license_count":
        result = await db.execute(
            query.offset((page - 1) * page_size).limit(page_size)
        )
        rows = result.all()
        contracts = [row[0] for row in rows]
    else:
        contracts = (
            await db.execute(
                query.offset((page - 1) * page_size).limit(page_size)
            )
        ).scalars().all()

    items = [await _build_contract_item(db, c) for c in contracts]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def get_contract(db: AsyncSession, contract_id: int) -> ContractListItem:
    """
    查询单个合同详情。

    输入参数：
        contract_id     合同 id
    输出：
        ContractListItem
    """
    logger.info(f"get_contract 入参: contract_id={contract_id}")
    contract = await _get_contract_or_404(db, contract_id)
    return await _build_contract_item(db, contract)


async def create_contract(db: AsyncSession, data: ContractCreate) -> ContractListItem:
    """
    新建合同。

    输入参数：
        data    ContractCreate（name/provider_id 必填，其余可选）
    输出：
        ContractListItem
    业务规则：
        - 名称不能重复
        - 同步写入 ContractPlatform 关联记录
    """
    logger.info(f"create_contract 入参: data={data}")
    # 校验名称不能仅包含空格
    if data.name and data.name.strip() == '':
        raise BusinessException(ErrorCode.CONTRACT_NAME_WHITESPACE, get_msg("CONTRACT_NAME_WHITESPACE"))
    await _check_name_unique(db, data.name)

    contract = Contract(
        name=data.name.strip(),
        provider_id=data.provider_id,
        start_date=data.start_date,
        end_date=data.end_date,
        notes=data.notes,
    )
    db.add(contract)
    await db.flush()

    for p in data.platforms:
        db.add(ContractPlatform(
            contract_id=contract.id,
            platform=p.platform,
            commercial_rights=p.commercial_rights,
        ))

    await db.commit()
    await db.refresh(contract)
    return await _build_contract_item(db, contract)


async def update_contract(
    db: AsyncSession, contract_id: int, data: ContractUpdate
) -> ContractListItem:
    """
    更新合同。

    输入参数：
        contract_id     合同 id
        data            ContractUpdate（所有字段均可选）
    输出：
        更新后的 ContractListItem
    """
    logger.info(f"update_contract 入参: contract_id={contract_id}, data={data}")
    contract = await _get_contract_or_404(db, contract_id)

    if data.name is not None and data.name != contract.name:
        # 校验名称不能仅包含空格
        if data.name.strip() == '':
            raise BusinessException(ErrorCode.CONTRACT_NAME_WHITESPACE, get_msg("CONTRACT_NAME_WHITESPACE"))
        await _check_name_unique(db, data.name, exclude_id=contract_id)
        contract.name = data.name.strip()
    if data.provider_id is not None:
        contract.provider_id = data.provider_id
    if data.start_date is not None:
        contract.start_date = data.start_date
    if data.end_date is not None:
        contract.end_date = data.end_date
    if data.notes is not None:
        contract.notes = data.notes
    if data.platforms is not None:
        await _update_platforms(db, contract_id, data.platforms)

    await db.commit()
    await db.refresh(contract)
    return await _build_contract_item(db, contract)


async def delete_contract(db: AsyncSession, contract_id: int) -> None:
    """
    软删除合同（is_deleted=True）。

    输入参数：
        contract_id     合同 id
    业务规则：
        - 若存在未删除的许可证，则拒绝删除
    """
    logger.info(f"delete_contract 入参: contract_id={contract_id}")
    contract = await _get_contract_or_404(db, contract_id)
    has_licenses = (
        await db.execute(
            select(License.id).where(
                License.contract_id == contract_id,
                License.is_deleted.is_(False),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if has_licenses:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTRACT_HAS_LICENSES"))
    contract.is_deleted = True
    await db.commit()


async def batch_delete_contracts(db: AsyncSession, req: BatchDeleteRequest) -> int:
    """
    批量软删除合同。

    输入参数：
        req.ids     要删除的合同 id 列表
    输出：
        实际删除数量
    业务规则：
        - 若任一合同存在未删除许可证，则拒绝删除
    """
    logger.info(f"batch_delete_contracts 入参: req={req}")
    contracts = (
        await db.execute(
            select(Contract).where(
                Contract.id.in_(req.ids), Contract.is_deleted.is_(False)
            )
        )
    ).scalars().all()
    for c in contracts:
        has_licenses = (
            await db.execute(
                select(License.id).where(
                    License.contract_id == c.id,
                    License.is_deleted.is_(False),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if has_licenses:
            raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTRACT_HAS_LICENSES"))
    for c in contracts:
        c.is_deleted = True
    await db.commit()
    return len(contracts)


async def get_without_license_count(db: AsyncSession) -> int:
    """
    统计当前无关联许可证的合同数量（用于"Without License"快捷统计按钮）。

    输出：
        整数，无许可证合同数量
    """
    logger.info(f"get_without_license_count 入参: 无")
    # 使用 NOT EXISTS 替代 NOT IN，避免子查询返回 NULL 时的问题
    sub_license_exists = select(License).where(
        License.contract_id == Contract.id,
        License.is_deleted.is_(False),
    )
    result = await db.execute(
        select(func.count(Contract.id)).where(
            Contract.is_deleted.is_(False),
            ~sub_license_exists.exists(),
        )
    )
    return result.scalar_one()


async def get_all_contracts_simple(db: AsyncSession) -> list[ContractSimpleItem]:
    """
    获取所有合同简要列表（用于许可证表单的下拉候选项）。

    输出：
        ContractSimpleItem 列表
    """
    logger.info(f"get_all_contracts_simple 入参: 无")
    contracts = (
        await db.execute(
            select(Contract).where(Contract.is_deleted.is_(False)).order_by(Contract.name.asc())
        )
    ).scalars().all()
    logger.info(f"get_all_contracts_simple 出参: count={len(contracts)}")
    return [
        ContractSimpleItem(
            id=c.id,
            name=c.name,
            provider_id=c.provider_id,
            provider_name=c.provider.name if c.provider else "",
        )
        for c in contracts
    ]


async def get_contract_attachments(
    db: AsyncSession, contract_id: int
) -> list[ContractAttachment]:
    """
    查询合同附件列表。

    输入参数：
        contract_id     合同 id
    输出：
        ContractAttachment ORM 列表
    """
    logger.info(f"get_contract_attachments 入参: contract_id={contract_id}")
    await _get_contract_or_404(db, contract_id)
    result = (
        await db.execute(
            select(ContractAttachment)
            .where(ContractAttachment.contract_id == contract_id)
            .order_by(ContractAttachment.created_at.desc())
        )
    ).scalars().all()
    logger.info(f"get_contract_attachments 出参: count={len(result)}")
    return result


async def upload_attachment(
    db: AsyncSession,
    contract_id: int,
    file_path: str,
    file_name: str,
    file_size: int,
    user_id: int | None,
    relative_path: str | None = None,
) -> ContractAttachment:
    """
    创建合同附件记录（文件已通过通用上传接口 /attachments/upload 上传）。

    输入参数：
        contract_id     合同 id
        file_path       加密全路径（存储用）
        file_name       原始文件名
        file_size       文件大小（字节）
        user_id         上传人 id（FK → cms_user.id）
        relative_path   相对路径（下载用）
    输出：
        ContractAttachment ORM 对象
    业务规则：
        - 合同必须存在且未删除
        - 文件已通过通用接口上传到 contracts 目录
    """
    logger.info(f"upload_attachment 入参: contract_id={contract_id}, file_path={file_path}, file_name={file_name}, file_size={file_size}, user_id={user_id}, relative_path={relative_path}")
    await _get_contract_or_404(db, contract_id)
    attachment = ContractAttachment(
        contract_id=contract_id,
        file_name=file_name,
        file_path=file_path,
        relative_path=relative_path,
        file_size=file_size,
        uploaded_by=user_id,
    )
    db.add(attachment)
    await db.commit()
    await db.refresh(attachment)
    return attachment


async def get_attachment(
    db: AsyncSession, contract_id: int, attachment_id: int
) -> ContractAttachment:
    """
    查询单个附件记录。

    输入参数：
        contract_id     合同 id
        attachment_id   附件 id
    输出：
        ContractAttachment ORM 对象（不存在则抛 404）
    """
    logger.info(f"get_attachment 入参: contract_id={contract_id}, attachment_id={attachment_id}")
    attachment = (
        await db.execute(
            select(ContractAttachment).where(
                ContractAttachment.id == attachment_id,
                ContractAttachment.contract_id == contract_id,
            )
        )
    ).scalar_one_or_none()
    if not attachment:
        raise NotFoundException(ErrorCode.ATTACHMENT_NOT_FOUND, get_msg("ATTACHMENT_NOT_FOUND"))
    return attachment


async def delete_attachment(db: AsyncSession, contract_id: int, attachment_id: int) -> None:
    """
    删除合同附件。

    输入参数：
        contract_id     合同 id
        attachment_id   附件 id
    """
    logger.info(f"delete_attachment 入参: contract_id={contract_id}, attachment_id={attachment_id}")
    await _get_contract_or_404(db, contract_id)
    attachment = (
        await db.execute(
            select(ContractAttachment).where(
                ContractAttachment.id == attachment_id,
                ContractAttachment.contract_id == contract_id,
            )
        )
    ).scalar_one_or_none()
    if not attachment:
        raise NotFoundException(ErrorCode.ATTACHMENT_NOT_FOUND, get_msg("ATTACHMENT_NOT_FOUND"))
    await db.delete(attachment)
    await db.commit()


# ─── Contract History ─────────────────────────────────────────

async def list_contract_history(
    db: AsyncSession, contract_id: int, limit: int = 100
) -> list[ContractHistoryItem]:
    """
    查询合同的操作历史（基于 operation_log 表）。

    优先使用 entity_type='contract' + entity_id 精确查询，
    兼容旧数据回退到 operation_type + operation_object/operation_content 模糊匹配。

    按 operation_time 倒序返回，最多 limit 条。
    """
    logger.info(f"list_contract_history 入参: contract_id={contract_id}, limit={limit}")
    from sqlalchemy import or_
    from app.internal.cms_biz_system.models.operation_log import OperationLog
    from app.internal.cms_biz_system.services.operation_log_service import OperationType

    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.entity_type == "contract",
                OperationLog.entity_id == contract_id,
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()

    if logs:
        return [
            ContractHistoryItem(
                id=log.id,
                processed_at=log.operation_time,
                processed_by=log.user_name,
                processed_type=log.operation_type,
                details=log.operation_content,
                previous_value=log.previous_value,
                updated_value=log.updated_value,
            )
            for log in logs
        ]

    contract = await _get_contract_or_404(db, contract_id)
    name = contract.name

    legacy_ops = [
        "合同创建",
        "合同编辑",
        "合同删除",
        "合同附件删除",
    ]

    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.operation_type.in_(legacy_ops),
                or_(
                    OperationLog.operation_object.ilike(f"%{name}%"),
                    OperationLog.operation_content.ilike(f"%ID={contract_id}%"),
                    OperationLog.operation_content.ilike(f"%contract ID={contract_id}%"),
                    OperationLog.operation_content.ilike(f"%[{contract_id},%"),
                    OperationLog.operation_content.ilike(f"%[{contract_id}]%"),
                    OperationLog.operation_content.ilike(f"%, {contract_id},%"),
                    OperationLog.operation_content.ilike(f"%, {contract_id}]%"),
                ),
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()

    return [
        ContractHistoryItem(
            id=log.id,
            processed_at=log.operation_time,
            processed_by=log.user_name,
            processed_type=log.operation_type,
            details=log.operation_content,
            previous_value=log.previous_value,
            updated_value=log.updated_value,
        )
        for log in logs
    ]
