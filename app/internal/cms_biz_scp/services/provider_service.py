"""
供应商（Provider）业务逻辑层。

职责：
- Provider 的 CRUD（新增、查询、编辑、软删除、批量软删除）
- 获取供应商简要列表（用于合同表单下拉）
- 查询供应商关联合同列表

业务规则：
1. 供应商名称全局唯一，重复时抛出 400 错误
2. 软删除：is_deleted=True，不物理删除
3. l1/l2/l3_assignee_id 均为可选的用户外键；review_level 控制显示层数
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.trade import Contract, Provider
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_scp.schemas.trade import (
    ContractListItem,
    ContractPlatformItem,
    ProviderCreate,
    ProviderHistoryItem,
    ProviderListItem,
    ProviderSimpleItem,
    ProviderUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from loguru import logger
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _generate_provider_code(db: AsyncSession) -> str:
    """自动生成供应商编码，格式：SUP + 年月日 + 4位序号。"""
    from datetime import datetime
    date_prefix = datetime.now().strftime("SUP%Y%m%d")
    # 查询当天已存在的编码数量（排除已删除数据）
    count_result = await db.execute(
        select(func.count()).select_from(Provider).where(
            Provider.provider_code.isnot(None),
            Provider.provider_code.like(f"{date_prefix}%"),
            Provider.is_deleted.is_(False)
        )
    )
    count = count_result.scalar_one()
    # 生成序号（从0001开始）
    sequence = count + 1
    return f"{date_prefix}{sequence:04d}"


async def _check_code_unique(db: AsyncSession, code: str, exclude_id: int | None = None) -> None:
    """校验供应商编码唯一性。"""
    query = select(Provider.id).where(
        Provider.provider_code == code,
        Provider.is_deleted.is_(False)
    )
    if exclude_id is not None:
        query = query.where(Provider.id != exclude_id)
    # 使用 limit(1) 避免数据库中存在多条同名记录时报错
    if (await db.execute(query.limit(1))).scalar_one_or_none():
        raise BusinessException(ErrorCode.PROVIDER_CODE_EXISTS, get_msg("PROVIDER_CODE_EXISTS"))


async def _get_user_name(db: AsyncSession, user_id: int | None) -> str | None:
    """查询用户显示名称（display_name 或 username）。"""
    if user_id is None:
        return None
    user = (await db.execute(
        select(User).where(User.id == user_id, User.is_deleted.is_(False)).limit(1)
    )).scalar_one_or_none()
    if not user:
        return None
    return user.display_name or user.username


async def _build_item(db: AsyncSession, provider: Provider) -> ProviderListItem:
    """将 ORM 对象转换为响应 schema，附加用户名称和合同数量。"""
    contract_count = (
        await db.execute(
            select(func.count(Contract.id)).where(
                Contract.provider_id == provider.id,
                Contract.is_deleted.is_(False),
            )
        )
    ).scalar_one()

    return ProviderListItem(
        id=provider.id,
        provider_code=provider.provider_code,
        name=provider.name,
        country=provider.country,
        review_level=provider.review_level,
        l1_assignee_id=provider.l1_assignee_id,
        l2_assignee_id=provider.l2_assignee_id,
        l3_assignee_id=provider.l3_assignee_id,
        l1_assignee_name=await _get_user_name(db, provider.l1_assignee_id),
        l2_assignee_name=await _get_user_name(db, provider.l2_assignee_id),
        l3_assignee_name=await _get_user_name(db, provider.l3_assignee_id),
        notes=provider.notes,
        contract_count=contract_count,
        created_at=provider.created_at,
    )


async def _get_provider_or_404(db: AsyncSession, provider_id: int) -> Provider:
    """查询供应商，不存在则抛 404。"""
    result = await db.execute(
        select(Provider).where(Provider.id == provider_id, Provider.is_deleted.is_(False))
    )
    provider = result.scalar_one_or_none()
    if not provider:
        raise NotFoundException(ErrorCode.PROVIDER_NOT_FOUND, get_msg("PROVIDER_NOT_FOUND"))
    return provider


async def _check_name_unique(db: AsyncSession, name: str, exclude_id: int | None = None) -> None:
    """校验供应商名称唯一性。"""
    query = select(Provider.id).where(Provider.name == name, Provider.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(Provider.id != exclude_id)
    # 使用 limit(1) 避免数据库中存在多条同名记录时报错
    if (await db.execute(query.limit(1))).scalar_one_or_none():
        raise BusinessException(ErrorCode.PROVIDER_NAME_EXISTS, get_msg("PROVIDER_NAME_EXISTS"))


# ─── Provider CRUD ────────────────────────────────────────────────────

async def list_providers(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    provider_code: str | None = None,
    name: str | None = None,
    country: str | None = None,
    notes: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[ProviderListItem]:
    """
    查询供应商列表（分页）。

    输入参数：
        page        页码（从 1 开始）
        page_size   每页条数
        provider_code 供应商编码关键字（模糊匹配）
        name        供应商名称关键字（模糊匹配）
        country     国家/地区关键字（模糊匹配）
        notes       备注关键字（模糊匹配）
        sort_by     排序字段（name, country, notes）
        sort_order  排序方向（asc, desc）

    输出：
        PaginatedResponse[ProviderListItem]
    """
    logger.info(f"list_providers 入参: page={page}, page_size={page_size}, provider_code={provider_code}, name={name}, country={country}, notes={notes}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(Provider).where(Provider.is_deleted.is_(False))

    if provider_code:
        query = query.where(Provider.provider_code.ilike(f"%{provider_code}%"))
    if name:
        query = query.where(Provider.name.ilike(f"%{name}%"))
    if country:
        query = query.where(Provider.country.ilike(f"%{country}%"))
    if notes:
        query = query.where(Provider.notes.ilike(f"%{notes}%"))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()

    # 动态排序（默认按创建时间倒序）
    if sort_by and sort_order:
        sort_column = getattr(Provider, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func)
        else:
            query = query.order_by(Provider.created_at.desc())
    else:
        query = query.order_by(Provider.created_at.desc())

    providers = (
        await db.execute(
            query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    items = [await _build_item(db, p) for p in providers]
    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def get_provider(db: AsyncSession, provider_id: int) -> ProviderListItem:
    """
    查询单个供应商详情。

    输入参数：
        provider_id     供应商 id
    输出：
        ProviderListItem
    """
    logger.info(f"get_provider 入参: provider_id={provider_id}")
    provider = await _get_provider_or_404(db, provider_id)
    return await _build_item(db, provider)


async def create_provider(db: AsyncSession, data: ProviderCreate) -> ProviderListItem:
    """
    新建供应商。

    输入参数：
        data    ProviderCreate（name 必填，其余可选）
    输出：
        ProviderListItem
    业务规则：
        - 供应商数量不超过限制值（-1 表示不限制）
        - 名称不能重复
    """
    logger.info(f"create_provider 入参: data={data}")
    # ── 供应商数量限制校验 ──
    from app.internal.cms_biz_system.services.usage_limit_service import get_limit_value

    limit = await get_limit_value(db, "supplier_count", -1)
    if limit != -1:
        current_count = (
            await db.execute(
                select(func.count()).select_from(Provider).where(Provider.is_deleted.is_(False))
            )
        ).scalar_one()
        if current_count >= limit:
            raise BusinessException(ErrorCode.SUPPLIER_LIMIT_EXCEEDED, get_msg("SUPPLIER_LIMIT_EXCEEDED"))

    # 校验名称不能仅包含空格
    if data.name and data.name.strip() == '':
        raise BusinessException(ErrorCode.PROVIDER_NAME_WHITESPACE, get_msg("PROVIDER_NAME_WHITESPACE"))

    await _check_name_unique(db, data.name)

    # 处理供应商编码：用户未提供则自动生成，提供了则校验唯一性
    provider_code = data.provider_code
    if provider_code:
        provider_code = provider_code.strip()
        await _check_code_unique(db, provider_code)
    else:
        provider_code = await _generate_provider_code(db)

    provider = Provider(
        provider_code=provider_code,
        name=data.name.strip(),
        country=data.country,
        review_level=data.review_level,
        l1_assignee_id=data.l1_assignee_id,
        l2_assignee_id=data.l2_assignee_id,
        l3_assignee_id=data.l3_assignee_id,
        notes=data.notes,
    )
    db.add(provider)
    await db.commit()
    await db.refresh(provider)
    return await _build_item(db, provider)


async def update_provider(
    db: AsyncSession, provider_id: int, data: ProviderUpdate
) -> ProviderListItem:
    """
    更新供应商。

    输入参数：
        provider_id     供应商 id
        data            ProviderUpdate（所有字段均可选）
    输出：
        更新后的 ProviderListItem
    """
    logger.info(f"update_provider 入参: provider_id={provider_id}, data={data}")
    provider = await _get_provider_or_404(db, provider_id)

    if data.name is not None and data.name != provider.name:
        # 校验名称不能仅包含空格
        if data.name.strip() == '':
            raise BusinessException(
                get_msg("PROVIDER_NAME_WHITESPACE"),
            )
        await _check_name_unique(db, data.name, exclude_id=provider_id)
        provider.name = data.name.strip()
    if data.provider_code is not None and data.provider_code != provider.provider_code:
        # 校验编码唯一性
        provider_code = data.provider_code.strip()
        if provider_code == '':
            raise BusinessException(ErrorCode.PROVIDER_CODE_EMPTY, get_msg("PROVIDER_CODE_EMPTY"))
        await _check_code_unique(db, provider_code, exclude_id=provider_id)
        provider.provider_code = provider_code
    if data.country is not None:
        provider.country = data.country
    if data.review_level is not None:
        provider.review_level = data.review_level
    if 'l1_assignee_id' in data.model_fields_set:
        provider.l1_assignee_id = data.l1_assignee_id
    if 'l2_assignee_id' in data.model_fields_set:
        provider.l2_assignee_id = data.l2_assignee_id
    if 'l3_assignee_id' in data.model_fields_set:
        provider.l3_assignee_id = data.l3_assignee_id
    if data.notes is not None:
        provider.notes = data.notes

    await db.commit()
    await db.refresh(provider)
    return await _build_item(db, provider)


async def delete_provider(db: AsyncSession, provider_id: int) -> None:
    """
    软删除供应商（is_deleted=True）。

    输入参数：
        provider_id     供应商 id
    业务规则：
        - 若存在未删除合同，则拒绝删除
    """
    logger.info(f"delete_provider 入参: provider_id={provider_id}")
    provider = await _get_provider_or_404(db, provider_id)
    has_contracts = (
        await db.execute(
            select(Contract.id).where(
                Contract.provider_id == provider_id,
                Contract.is_deleted.is_(False),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if has_contracts:
        raise BusinessException(ErrorCode.PROVIDER_HAS_CONTRACTS, get_msg("PROVIDER_HAS_CONTRACTS"))
    provider.is_deleted = True
    await db.commit()


async def batch_delete_providers(db: AsyncSession, req: BatchDeleteRequest) -> int:
    """
    批量软删除供应商。

    输入参数：
        req.ids     要删除的供应商 id 列表
    输出：
        实际删除数量
    业务规则：
        - 若任一供应商存在未删除合同，则拒绝删除
    """
    logger.info(f"batch_delete_providers 入参: req={req}")
    providers = (
        await db.execute(
            select(Provider).where(
                Provider.id.in_(req.ids), Provider.is_deleted.is_(False)
            )
        )
    ).scalars().all()
    for p in providers:
        has_contracts = (
            await db.execute(
                select(Contract.id).where(
                    Contract.provider_id == p.id,
                    Contract.is_deleted.is_(False),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if has_contracts:
            raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("PROVIDER_HAS_CONTRACTS"))
    for p in providers:
        p.is_deleted = True
    await db.commit()
    return len(providers)


async def list_provider_contracts(
    db: AsyncSession, provider_id: int
) -> list[ContractListItem]:
    """
    查询供应商下的合同列表（不分页）。

    输入参数：
        provider_id     供应商 id
    输出：
        ContractListItem 列表
    """
    logger.info(f"list_provider_contracts 入参: provider_id={provider_id}")
    from app.internal.cms_biz_scp.services.contract_service import _build_contract_item  # 避免循环导入
    await _get_provider_or_404(db, provider_id)
    contracts = (
        await db.execute(
            select(Contract).where(
                Contract.provider_id == provider_id,
                Contract.is_deleted.is_(False),
            ).order_by(Contract.id.desc())
        )
    ).scalars().all()
    return [await _build_contract_item(db, c) for c in contracts]


async def get_all_providers_simple(db: AsyncSession) -> list[ProviderSimpleItem]:
    """
    获取所有供应商简要列表（用于合同表单的下拉候选项）。

    输出：
        ProviderSimpleItem 列表（id + name）
    """
    logger.info(f"get_all_providers_simple 入参: 无")
    providers = (
        await db.execute(
            select(Provider).where(Provider.is_deleted.is_(False)).order_by(Provider.name.asc())
        )
    ).scalars().all()
    return [ProviderSimpleItem(id=p.id, name=p.name) for p in providers]


async def list_provider_history(
    db: AsyncSession, provider_id: int, limit: int = 100
) -> list[ProviderHistoryItem]:
    """
    查询供应商的操作历史（Processed History）。

    优先使用 entity_type='provider' + entity_id 精确查询，
    兼容旧数据回退到 operation_type + operation_object/operation_content 模糊匹配。

    按 operation_time 倒序返回前 limit 条。
    """
    logger.info(f"list_provider_history 入参: provider_id={provider_id}")
    from sqlalchemy import or_
    from app.internal.cms_biz_system.models.operation_log import OperationLog
    from app.internal.cms_biz_system.services.operation_log_service import OperationType

    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.entity_type == "provider",
                OperationLog.entity_id == provider_id,
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()

    if logs:
        return [
            ProviderHistoryItem(
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

    provider = await _get_provider_or_404(db, provider_id)
    name = provider.name

    legacy_ops = [
        "供应商创建",
        "供应商编辑",
        "供应商删除",
    ]

    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.operation_type.in_(legacy_ops),
                or_(
                    OperationLog.operation_object.ilike(f"%{name}%"),
                    OperationLog.operation_content.ilike(f"%ID={provider_id}%"),
                    OperationLog.operation_content.ilike(f"%[{provider_id},%"),
                    OperationLog.operation_content.ilike(f"%[{provider_id}]%"),
                    OperationLog.operation_content.ilike(f"%, {provider_id},%"),
                    OperationLog.operation_content.ilike(f"%, {provider_id}]%"),
                ),
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()

    return [
        ProviderHistoryItem(
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
