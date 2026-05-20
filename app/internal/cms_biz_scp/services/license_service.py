"""
许可证（License）业务逻辑层。

职责：
- License 的 CRUD（新增、查询、编辑、软删除、批量软删除）
- 许可证平台关系维护（LicensePlatform）
- 许可证内容关联管理（LicenseContent）：新增关联、移除关联、查询已关联内容
- 查询可供选择的内容列表（Add Content to License 弹框数据源）
- 统计无内容的许可证数量

业务规则：
1. 许可证名称全局唯一，重复时抛出 400 错误
2. 更新平台时，先删除旧记录再批量插入新记录
3. 软删除：is_deleted=True，不物理删除
4. regions 存储为逗号分隔字符串，读写时转换为 list[str]
5. 同一内容不能重复添加至同一许可证（唯一约束保证，重复时幂等跳过）
"""

from datetime import date as date_type

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_package.models.package import Content
from ..models.trade import (
    Contract,
    License,
    LicenseContent,
    LicensePlatform,
)
from app.internal.cms_biz_scp.schemas.trade import (
    ContentAddToLicenseRequest,
    ContentForTradeItem,
    LicenseCreate,
    LicenseHistoryItem,
    LicenseListItem,
    LicensePlatformItem,
    LicenseSimpleItem,
    LicenseUpdate,
)
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from loguru import logger
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _get_genre_name(db: AsyncSession, genre_id: int | None) -> str | None:
    """根据 genre_id 查询题材名称，id 为空或不存在时返回 None。"""
    if genre_id is None:
        return None
    genre = (
        await db.execute(select(Genre).where(Genre.id == genre_id, Genre.is_deleted.is_(False)))
    ).scalar_one_or_none()
    return genre.name if genre else None

def _regions_to_list(regions: str | None) -> list[str]:
    """将存储的逗号分隔地区字符串转换为列表。"""
    if not regions:
        return []
    return [r.strip() for r in regions.split(",") if r.strip()]


def _regions_to_str(regions: list[str]) -> str | None:
    """将地区列表转换为逗号分隔字符串。"""
    if not regions:
        return None
    return ",".join(regions)


def _calculate_license_status(lic: License) -> str:
    """根据日期自动计算许可证状态。"""
    if lic.is_deleted:
        return "DELETED"
    today = date_type.today()
    if lic.start_date and lic.start_date > today:
        return "INACTIVE"
    if lic.end_date and lic.end_date < today:
        return "EXPIRED"
    return "ACTIVE"


async def _build_license_item(db: AsyncSession, lic: License) -> LicenseListItem:
    """将 ORM 对象转换为响应 schema，附加内容数量和供应商信息。"""
    content_count = (
        await db.execute(
            select(func.count(LicenseContent.id)).where(
                LicenseContent.license_id == lic.id,
                LicenseContent.is_deleted.is_(False),
            )
        )
    ).scalar_one()

    contract = lic.contract
    provider_id = contract.provider_id if contract else 0
    provider_name = (contract.provider.name if contract and contract.provider else "") if contract else ""

    # 根据日期自动计算状态
    calculated_status = _calculate_license_status(lic)

    return LicenseListItem(
        id=lic.id,
        name=lic.name,
        contract_id=lic.contract_id,
        contract_name=contract.name if contract else "",
        provider_id=provider_id,
        provider_name=provider_name,
        service_type=lic.service_type,
        platforms=[
            LicensePlatformItem(platform=p.platform, ad_rights=p.ad_rights)
            for p in lic.platforms
        ],
        regions=_regions_to_list(lic.regions),
        start_date=lic.start_date,
        end_date=lic.end_date,
        status=calculated_status,
        mobile_download=lic.mobile_download,
        download_duration=lic.download_duration,
        mobile_preview=lic.mobile_preview,
        preview_begin_time=lic.preview_begin_time,
        preview_end_time=lic.preview_end_time,
        notes=lic.notes,
        content_count=content_count,
        created_at=lic.created_at,
    )


async def _get_license_or_404(db: AsyncSession, license_id: int) -> License:
    """查询许可证，不存在则抛 404。"""
    lic = (
        await db.execute(
            select(License).where(License.id == license_id, License.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if not lic:
        raise NotFoundException(ErrorCode.LICENSE_NOT_FOUND, get_msg("LICENSE_NOT_FOUND"))
    return lic


async def _check_name_unique(db: AsyncSession, name: str, exclude_id: int | None = None) -> None:
    """校验许可证名称唯一性。"""
    query = select(License.id).where(License.name == name, License.is_deleted.is_(False))
    if exclude_id is not None:
        query = query.where(License.id != exclude_id)
    # 使用 limit(1) 避免数据库中存在多条同名记录时报错
    if (await db.execute(query.limit(1))).scalar_one_or_none():
        raise BusinessException(ErrorCode.LICENSE_NAME_EXISTS, get_msg("LICENSE_NAME_EXISTS"))


async def _update_platforms(
    db: AsyncSession, license_id: int, platforms: list[LicensePlatformItem]
) -> None:
    """删除旧平台记录后批量插入新记录。"""
    old = (
        await db.execute(
            select(LicensePlatform).where(LicensePlatform.license_id == license_id)
        )
    ).scalars().all()
    for old_p in old:
        await db.delete(old_p)
    await db.flush()
    for p in platforms:
        db.add(LicensePlatform(
            license_id=license_id,
            platform=p.platform,
            ad_rights=p.ad_rights,
        ))


# ─── License CRUD ─────────────────────────────────────────────────────

async def list_licenses(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    name: str | None = None,
    service_types: list[str] | None = None,
    platforms: list[str] | None = None,
    statuses: list[str] | None = None,
    start_date_from: str | None = None,
    start_date_to: str | None = None,
    end_date_from: str | None = None,
    end_date_to: str | None = None,
    contract_id: int | None = None,
    provider_id: int | None = None,
    without_content: bool = False,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[LicenseListItem]:
    """
    查询许可证列表（分页）。

    输入参数：
        page                页码（从 1 开始）
        page_size           每页条数
        name                许可证名称关键字（模糊匹配）
        service_types       服务类型列表（任一匹配）
        platforms           平台列表（任一匹配）
        statuses            状态列表（任一匹配）
        start_date_from     开始日期范围下限
        start_date_to       开始日期范围上限
        end_date_from       结束日期范围下限
        end_date_to         结束日期范围上限
        contract_id         仅返回该合同下的许可证（供合同详情页使用）
        provider_id         仅返回该供应商关联合同下的许可证
        without_content     仅返回未关联任何内容的许可证

    输出：
        PaginatedResponse[LicenseListItem]
    """
    logger.info(f"list_licenses 入参: page={page}, page_size={page_size}, name={name}, service_types={service_types}, platforms={platforms}, statuses={statuses}, start_date_from={start_date_from}, start_date_to={start_date_to}, end_date_from={end_date_from}, end_date_to={end_date_to}, contract_id={contract_id}, provider_id={provider_id}, without_content={without_content}, sort_by={sort_by}, sort_order={sort_order}")
    query = select(License).where(License.is_deleted.is_(False))

    if name:
        query = query.where(License.name.ilike(f"%{name}%"))
    if service_types:
        query = query.where(License.service_type.in_(service_types))
    # NOTE: statuses 不在此处理，因为 status 是动态计算的，需要在获取数据后过滤
    if platforms:
        sub = select(LicensePlatform.license_id).where(
            LicensePlatform.platform.in_(platforms)
        )
        query = query.where(License.id.in_(sub))
    if start_date_from:
        query = query.where(License.start_date >= date_type.fromisoformat(start_date_from))
    if start_date_to:
        query = query.where(License.start_date <= date_type.fromisoformat(start_date_to))
    if end_date_from:
        query = query.where(License.end_date >= date_type.fromisoformat(end_date_from))
    if end_date_to:
        query = query.where(License.end_date <= date_type.fromisoformat(end_date_to))
    if contract_id is not None:
        query = query.where(License.contract_id == contract_id)
    if provider_id is not None:
        sub_con = select(Contract.id).where(
            Contract.provider_id == provider_id,
            Contract.is_deleted.is_(False),
        )
        query = query.where(License.contract_id.in_(sub_con))
    if without_content:
        sub_content = select(LicenseContent.license_id).where(LicenseContent.is_deleted.is_(False))
        query = query.where(License.id.notin_(sub_content))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(License, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, License.id.desc())
        else:
            query = query.order_by(License.id.desc())
    else:
        query = query.order_by(License.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    licenses = (
        await db.execute(
            query.offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    # 先转换为响应项，根据动态计算的 status 过滤，再分页
    all_items = [await _build_license_item(db, lic) for lic in licenses]

    # 根据动态计算的 status 过滤
    if statuses:
        filtered_items = [item for item in all_items if item.status in statuses]
    else:
        filtered_items = all_items

    # 修正分页返回的 total
    actual_total = len(filtered_items) if statuses else total

    return PaginatedResponse(total=actual_total, page=page, page_size=page_size, items=filtered_items)


async def get_license(db: AsyncSession, license_id: int) -> LicenseListItem:
    """
    查询单个许可证详情。

    输入参数：
        license_id      许可证 id
    输出：
        LicenseListItem
    """
    logger.info(f"get_license 入参: license_id={license_id}")
    lic = await _get_license_or_404(db, license_id)
    return await _build_license_item(db, lic)


async def create_license(db: AsyncSession, data: LicenseCreate) -> LicenseListItem:
    """
    新建许可证。

    输入参数：
        data    LicenseCreate（name/contract_id/service_type 必填）
    输出：
        LicenseListItem
    业务规则：
        - 名称不能重复
        - 同步写入 LicensePlatform 关联记录
        - regions 转换为逗号分隔字符串存储
    """
    logger.info(f"create_license 入参: data={data}")
    await _check_name_unique(db, data.name)

    lic = License(
        name=data.name,
        contract_id=data.contract_id,
        service_type=data.service_type,
        regions=_regions_to_str(data.regions),
        start_date=data.start_date,
        end_date=data.end_date,
        mobile_download=data.mobile_download,
        download_duration=data.download_duration,
        mobile_preview=data.mobile_preview,
        preview_begin_time=data.preview_begin_time,
        preview_end_time=data.preview_end_time,
        notes=data.notes,
    )
    db.add(lic)
    await db.flush()

    for p in data.platforms:
        db.add(LicensePlatform(
            license_id=lic.id,
            platform=p.platform,
            ad_rights=p.ad_rights,
        ))

    await db.commit()
    await db.refresh(lic)
    return await _build_license_item(db, lic)


async def update_license(
    db: AsyncSession, license_id: int, data: LicenseUpdate
) -> LicenseListItem:
    """
    更新许可证。

    输入参数：
        license_id      许可证 id
        data            LicenseUpdate（所有字段均可选）
    输出：
        更新后的 LicenseListItem
    """
    logger.info(f"update_license 入参: license_id={license_id}, data={data}")
    lic = await _get_license_or_404(db, license_id)

    if data.name is not None and data.name != lic.name:
        await _check_name_unique(db, data.name, exclude_id=license_id)
        lic.name = data.name
    if data.unlink_contract:
        lic.contract_id = None
    elif data.contract_id is not None:
        lic.contract_id = data.contract_id
    if data.service_type is not None:
        lic.service_type = data.service_type
    if data.regions is not None:
        lic.regions = _regions_to_str(data.regions)
    if data.start_date is not None:
        lic.start_date = data.start_date
    if data.end_date is not None:
        lic.end_date = data.end_date
    if data.mobile_download is not None:
        lic.mobile_download = data.mobile_download
    if data.download_duration is not None:
        lic.download_duration = data.download_duration
    if data.mobile_preview is not None:
        lic.mobile_preview = data.mobile_preview
    if data.preview_begin_time is not None:
        lic.preview_begin_time = data.preview_begin_time
    if data.preview_end_time is not None:
        lic.preview_end_time = data.preview_end_time
    if data.notes is not None:
        lic.notes = data.notes
    if data.platforms is not None:
        await _update_platforms(db, license_id, data.platforms)

    await db.commit()
    await db.refresh(lic)
    return await _build_license_item(db, lic)


async def delete_license(db: AsyncSession, license_id: int) -> None:
    """
    软删除许可证（is_deleted=True）。

    输入参数：
        license_id      许可证 id
    业务规则：
        - 若存在未删除的内容关联，则拒绝删除
    """
    logger.info(f"delete_license 入参: license_id={license_id}")
    lic = await _get_license_or_404(db, license_id)
    has_contents = (
        await db.execute(
            select(LicenseContent.id).where(
                LicenseContent.license_id == license_id,
                LicenseContent.is_deleted.is_(False),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if has_contents:
        raise BusinessException(ErrorCode.LICENSE_HAS_CONTENTS, get_msg("LICENSE_HAS_CONTENTS"))
    lic.is_deleted = True
    await db.commit()


async def batch_delete_licenses(db: AsyncSession, req: BatchDeleteRequest) -> int:
    """
    批量软删除许可证。

    输入参数：
        req.ids     要删除的许可证 id 列表
    输出：
        实际删除数量
    业务规则：
        - 若任一许可证存在未删除的内容关联，则拒绝删除
    """
    logger.info(f"batch_delete_licenses 入参: req={req}")
    licenses = (
        await db.execute(
            select(License).where(
                License.id.in_(req.ids), License.is_deleted.is_(False)
            )
        )
    ).scalars().all()
    for lic in licenses:
        has_contents = (
            await db.execute(
                select(LicenseContent.id).where(
                    LicenseContent.license_id == lic.id,
                    LicenseContent.is_deleted.is_(False),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if has_contents:
            raise BusinessException(ErrorCode.LICENSE_HAS_CONTENTS, get_msg("LICENSE_HAS_CONTENTS"))
    for lic in licenses:
        lic.is_deleted = True
    await db.commit()
    return len(licenses)


async def get_contract_licenses_simple(
    db: AsyncSession, contract_id: int, name: str | None = None
) -> list[LicenseSimpleItem]:
    """
    获取合同下的许可证简要列表（用于"添加内容至合同"弹框的中栏）。

    输入参数：
        contract_id     合同 id
        name            许可证名称关键字（模糊匹配，可选）
    输出：
        LicenseSimpleItem 列表
    """
    logger.info(f"get_contract_licenses_simple 入参: contract_id={contract_id}, name={name}")
    query = select(License).where(
        License.contract_id == contract_id,
        License.is_deleted.is_(False),
    )
    if name:
        query = query.where(License.name.ilike(f"%{name}%"))
    query = query.order_by(License.id.desc())
    licenses = (await db.execute(query)).scalars().all()
    result = [
        LicenseSimpleItem(
            id=lic.id,
            name=lic.name,
            contract_id=lic.contract_id,
            service_type=lic.service_type,
            start_date=lic.start_date,
            end_date=lic.end_date,
        )
        for lic in licenses
    ]
    logger.info(f"get_contract_licenses_simple 出参: result={result}")
    return result


# ─── License↔Content 关联 ─────────────────────────────────────────────

async def list_license_contents(
    db: AsyncSession, license_id: int
) -> list[ContentForTradeItem]:
    """
    查询许可证已关联的内容列表。

    输入参数：
        license_id      许可证 id
    输出：
        ContentForTradeItem 列表（包含 genre/original_name/release_year，内容模块完整实现前为 None）
    """
    logger.info(f"list_license_contents 入参: license_id={license_id}")
    await _get_license_or_404(db, license_id)
    rows = (
        await db.execute(
            select(LicenseContent).where(
                LicenseContent.license_id == license_id,
                LicenseContent.is_deleted.is_(False),
            )
            .order_by(LicenseContent.created_at.desc())
        )
    ).scalars().all()

    result: list[ContentForTradeItem] = []
    for row in rows:
        c = (
            await db.execute(
                select(Content).where(Content.id == row.content_id, Content.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if c:
            result.append(ContentForTradeItem(
                id=c.id,
                content_type=c.content_type,
                title=c.title,
                status=c.status,
                license_names=[],
                genre=await _get_genre_name(db, c.genre_id),
                original_name=None,
                release_year=None,
            ))
    return result


async def add_contents_to_license(
    db: AsyncSession,
    license_id: int,
    req: ContentAddToLicenseRequest,
) -> list[ContentForTradeItem]:
    """
    向许可证添加内容关联（支持批量，幂等）。

    输入参数：
        license_id      许可证 id
        req.content_ids 要关联的内容 id 列表
    输出：
        关联成功后，返回当前许可证已关联内容列表
    """
    logger.info(f"add_contents_to_license 入参: license_id={license_id}, req={req}")
    await _get_license_or_404(db, license_id)

    existing_ids = set(
        (
            await db.execute(
                select(LicenseContent.content_id).where(
                    LicenseContent.license_id == license_id,
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
    )

    soft_deleted_rows = (
        await db.execute(
            select(LicenseContent).where(
                LicenseContent.license_id == license_id,
                LicenseContent.content_id.in_(req.content_ids),
                LicenseContent.is_deleted.is_(True),
            )
        )
    ).scalars().all()
    soft_deleted_map = {row.content_id: row for row in soft_deleted_rows}

    for cid in req.content_ids:
        if cid in existing_ids:
            continue
        content = (
            await db.execute(
                select(Content).where(Content.id == cid, Content.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if not content:
            raise BusinessException(ErrorCode.CONTENT_ID_NOT_FOUND, get_msg("CONTENT_ID_NOT_FOUND"))
        if cid in soft_deleted_map:
            soft_deleted_map[cid].is_deleted = False
        else:
            db.add(LicenseContent(license_id=license_id, content_id=cid))
        existing_ids.add(cid)

    await db.commit()
    return await list_license_contents(db, license_id)


async def remove_content_from_license(
    db: AsyncSession, license_id: int, content_id: int
) -> None:
    """
    从许可证移除内容关联（软删除）。

    输入参数：
        license_id      许可证 id
        content_id      要移除的内容 id
    """
    logger.info(f"remove_content_from_license 入参: license_id={license_id}, content_id={content_id}")
    await _get_license_or_404(db, license_id)
    row = (
        await db.execute(
            select(LicenseContent).where(
                LicenseContent.license_id == license_id,
                LicenseContent.content_id == content_id,
                LicenseContent.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if row:
        row.is_deleted = True
        await db.commit()


async def list_available_contents(
    db: AsyncSession,
    license_id: int,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
    genres: list[str] | None = None,
    without_license: bool = False,
) -> PaginatedResponse[ContentForTradeItem]:
    """
    查询可添加至许可证的内容列表（排除已关联内容）。

    输入参数：
        license_id      当前许可证 id（用于排除已关联内容）
        page            页码
        page_size       每页条数
        title           内容标题关键字（模糊匹配）
        content_types   内容类型过滤列表（MOVIE/EPISODE 等）
        ingest_statuses Ingest 状态过滤列表（对应 content.status）
        genres          题材过滤（内容模块完整实现后生效，当前为 no-op）
        without_license 仅返回未关联任何许可证的内容

    输出：
        PaginatedResponse[ContentForTradeItem]
    """
    logger.info(f"list_available_contents 入参: license_id={license_id}, page={page}, page_size={page_size}, title={title}, content_types={content_types}, ingest_statuses={ingest_statuses}, genres={genres}, without_license={without_license}")
    already_linked = set(
        (
            await db.execute(
                select(LicenseContent.content_id).where(
                    LicenseContent.license_id == license_id,
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
    )

    query = select(Content).where(Content.is_deleted.is_(False))

    if already_linked:
        query = query.where(Content.id.notin_(already_linked))
    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if ingest_statuses:
        query = query.where(Content.status.in_(ingest_statuses))
    # genres 过滤：内容模块完整实现前 no-op（content 表尚无 genre 字段）
    if without_license:
        sub_lc = select(LicenseContent.content_id).where(LicenseContent.is_deleted.is_(False))
        query = query.where(Content.id.notin_(sub_lc))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    contents = (
        await db.execute(
            query.order_by(Content.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    # 为每条内容查询其当前关联的许可证名称（仅供参考）
    items: list[ContentForTradeItem] = []
    for c in contents:
        lic_rows = (
            await db.execute(
                select(LicenseContent.license_id).where(
                    LicenseContent.content_id == c.id,
                    LicenseContent.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        lic_names: list[str] = []
        if lic_rows:
            lics = (
                await db.execute(
                    select(License.name).where(
                        License.id.in_(lic_rows),
                        License.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
            lic_names = list(lics)
        items.append(ContentForTradeItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            status=c.status,
            license_names=lic_names,
            genre=await _get_genre_name(db, c.genre_id),
            original_name=None,
            release_year=None,
        ))

    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def list_unlicensed_contents(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
    genres: list[str] | None = None,
) -> PaginatedResponse[ContentForTradeItem]:
    """
    查询未关联任何许可证的内容列表（用于"添加内容至合同"弹框左栏初始状态）。

    输入参数：
        page            页码
        page_size       每页条数
        title           内容标题关键字（模糊匹配）
        content_types   内容类型过滤列表
        ingest_statuses Ingest 状态过滤列表
        genres          题材过滤（当前为 no-op）

    输出：
        PaginatedResponse[ContentForTradeItem]
    """
    logger.info(f"list_unlicensed_contents 入参: page={page}, page_size={page_size}, title={title}, content_types={content_types}, ingest_statuses={ingest_statuses}, genres={genres}")
    query = select(Content).where(Content.is_deleted.is_(False))

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if ingest_statuses:
        query = query.where(Content.status.in_(ingest_statuses))
    # genres 过滤：内容模块完整实现前 no-op
    sub_lc = select(LicenseContent.content_id)
    query = query.where(Content.id.notin_(sub_lc))

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    contents = (
        await db.execute(
            query.order_by(Content.id.desc()).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()

    items: list[ContentForTradeItem] = []
    for c in contents:
        lic_rows = (
            await db.execute(
                select(LicenseContent.license_id).where(LicenseContent.content_id == c.id)
            )
        ).scalars().all()
        lic_names: list[str] = []
        if lic_rows:
            lics = (
                await db.execute(
                    select(License.name).where(
                        License.id.in_(lic_rows),
                        License.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
            lic_names = list(lics)
        items.append(ContentForTradeItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            status=c.status,
            license_names=lic_names,
            genre=await _get_genre_name(db, c.genre_id),
            original_name=None,
            release_year=None,
        ))

    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def get_without_license_content_count(db: AsyncSession) -> int:
    """
    统计当前未关联任何许可证的内容数量。

    输出：
        整数，无许可证内容数量
    """
    logger.info(f"get_without_license_content_count 入参: 无")
    sub_lc = select(LicenseContent.content_id)
    result = await db.execute(
        select(func.count(Content.id)).where(
            Content.is_deleted.is_(False),
            Content.id.notin_(sub_lc),
        )
    )
    return result.scalar_one()


async def list_license_history(
    db: AsyncSession, license_id: int, limit: int = 100
) -> list[LicenseHistoryItem]:
    """
    查询许可证的操作历史（基于 operation_log 表）。

    优先使用 entity_type='license' + entity_id 精确查询，
    兼容旧数据回退到 operation_type + operation_object/operation_content 模糊匹配。

    按 operation_time 倒序返回，最多 limit 条。
    """
    logger.info(f"list_license_history 入参: license_id={license_id}, limit={limit}")
    from sqlalchemy import or_
    from app.internal.cms_biz_system.models.operation_log import OperationLog
    from app.internal.cms_biz_system.services.operation_log_service import OperationType

    license_ops = [
        OperationType.LICENSE_CREATE,
        OperationType.LICENSE_EDIT,
        OperationType.LICENSE_DELETE,
        OperationType.LICENSE_CONTENT_ADD,
        OperationType.LICENSE_CONTENT_REMOVE,
    ]

    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.entity_type == "license",
                OperationLog.entity_id == license_id,
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()

    if logs:
        return [
            LicenseHistoryItem(
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

    lic = await _get_license_or_404(db, license_id)
    name = lic.name

    legacy_ops = [
        "许可证创建",
        "许可证编辑",
        "许可证删除",
        "许可证内容关联",
        "许可证内容移除",
    ]

    logs = (
        await db.execute(
            select(OperationLog)
            .where(
                OperationLog.is_deleted.is_(False),
                OperationLog.operation_type.in_(legacy_ops),
                or_(
                    OperationLog.operation_object.ilike(f"%{name}%"),
                    OperationLog.operation_object.ilike(f"%ID={license_id}%"),
                    OperationLog.operation_content.ilike(f"%ID={license_id}%"),
                    OperationLog.operation_content.ilike(f"%license ID={license_id}%"),
                    OperationLog.operation_content.ilike(f"%[{license_id},%"),
                    OperationLog.operation_content.ilike(f"%[{license_id}]%"),
                    OperationLog.operation_content.ilike(f"%, {license_id},%"),
                    OperationLog.operation_content.ilike(f"%, {license_id}]%"),
                ),
            )
            .order_by(OperationLog.operation_time.desc())
            .limit(limit)
        )
    ).scalars().all()

    return [
        LicenseHistoryItem(
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
