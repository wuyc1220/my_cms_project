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
6. 许可证层级继承：
   - 总季（SEASON）添加许可证时，自动传播给其下所有单季（SEASON_SERIES）和单集（EPISODE）
   - 单季（SEASON_SERIES）添加许可证时，自动传播给其下所有单集（EPISODE）
   - 单集独立添加许可证不影响父级
   - 总季/单季更换或解除许可证时，覆盖所有子节点的关联
"""

from datetime import date as date_type
import json

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_package.models.enums import ContentType
from app.internal.cms_biz_package.models.package import Content, ContentGenre
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
from app.internal.cms_biz_system.services.data_auth_filter import apply_content_data_auth
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _get_genre_names(db: AsyncSession, content_id: int) -> str | None:
    """查询题材名称列表（逗号分隔），从中间表 content_genre 获取。"""
    rows = (
        await db.execute(
            select(Genre.name)
            .join(ContentGenre, ContentGenre.genre_id == Genre.id)
            .where(ContentGenre.content_id == content_id, Genre.is_deleted.is_(False))
        )
    ).scalars().all()
    if not rows:
        return None
    return ", ".join(rows)

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
        raise BusinessException(ErrorCode.LICENSE_NAME_EXISTS, get_msg("LICENSE_NAME_EXISTS", name=name))


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


async def _get_season_descendant_ids(db: AsyncSession, season_id: int) -> list[int]:
    """
    获取 SEASON（总季）下所有子孙 content_id。

    层级：SEASON → SEASON_SERIES（单季）→ EPISODE（单集）

    返回所有 SEASON_SERIES 和 EPISODE 的 content_id 列表（扁平），
    不含 SEASON 自身。
    """
    # 第一层：SEASON_SERIES
    season_series_rows = (
        await db.execute(
            select(Content.id).where(
                Content.parent_id == season_id,
                Content.content_type == ContentType.SEASON_SERIES.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().all()
    season_series_ids = list(season_series_rows)

    if not season_series_ids:
        return []

    # 第二层：EPISODE（单集）
    episode_rows = (
        await db.execute(
            select(Content.id).where(
                Content.parent_id.in_(season_series_ids),
                Content.content_type == ContentType.EPISODE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().all()
    episode_ids = list(episode_rows)

    return season_series_ids + episode_ids


async def _get_season_series_child_ids(db: AsyncSession, season_series_id: int) -> list[int]:
    """
    获取 SEASON_SERIES（单季）下的所有 EPISODE（单集）content_id。

    不含 SEASON_SERIES 自身。
    """
    episode_rows = (
        await db.execute(
            select(Content.id).where(
                Content.parent_id == season_series_id,
                Content.content_type == ContentType.EPISODE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().all()
    return list(episode_rows)


async def _get_series_child_ids(db: AsyncSession, series_id: int) -> list[int]:
    """
    获取 SERIES（普通连续剧）下的所有 EPISODE（单集）content_id。

    不含 SERIES 自身。
    """
    episode_rows = (
        await db.execute(
            select(Content.id).where(
                Content.parent_id == series_id,
                Content.content_type == ContentType.EPISODE.value,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().all()
    return list(episode_rows)


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
    db: AsyncSession, license_id: int, current_user: User | None = None
) -> list[ContentForTradeItem]:
    """
    查询许可证已关联的内容列表。

    输入参数：
        license_id      许可证 id
        current_user    当前登录用户（本接口不做数据权限过滤，保留参数仅为路由层统一传入）
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

    content_ids = [row.content_id for row in rows]
    if not content_ids:
        return []

    query = select(Content).where(
        Content.id.in_(content_ids),
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    )
    # 详情页已关联列表不做内容数据权限过滤：
    # 许可证详情进入权由许可证模块权限控制，能进详情页就应看到它关联了什么；
    # 数据权限控制仅保留在“添加内容弹框”（list_available_contents），
    # 前端点详情跳转内容详情页时仍走数据权限校验（checkAndNavigateToContent）
    contents = (await db.execute(query)).scalars().all()

    result: list[ContentForTradeItem] = []
    for c in contents:
        result.append(ContentForTradeItem(
            id=c.id,
            content_type=c.content_type,
            title=c.title,
            status=c.status,
            license_names=[],
            genre=await _get_genre_names(db, c.id),
            original_name=None,
            release_year=None,
        ))
    return result


async def add_contents_to_license(
    db: AsyncSession,
    license_id: int,
    req: ContentAddToLicenseRequest,
    current_user: User | None = None,
) -> list[ContentForTradeItem]:
    """
    向许可证添加内容关联（支持批量，幂等）。

    输入参数：
        license_id      许可证 id
        req.content_ids 要关联的内容 id 列表
        current_user    当前登录用户（用于数据权限过滤）
    输出：
        关联成功后，返回当前许可证已关联内容列表

    业务规则：
        - 总季（SEASON）添加时，自动级联传播给其下所有单季和单集
        - 单季/单集独立添加不影响父级和其他兄弟节点
        - 幂等：已关联的内容不重复创建关联，但父级内容（SEASON/SEASON_SERIES/SERIES）
          即使已关联也会触发级联同步（补齐子节点缺失的许可证关联）
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

    # ── 收集 SEASON / SEASON_SERIES / SERIES 类型的内容 id，用于后续级联传播 ──
    season_content_ids: list[int] = []
    season_series_content_ids: list[int] = []
    series_content_ids: list[int] = []

    # 本次实际新增关联的内容 id（用于状态回退）
    newly_linked_ids: list[int] = []

    for cid in req.content_ids:
        # 先查询内容并收集类型（须在幂等跳过之前）：
        # 即使许可证已关联该内容（cid in existing_ids），若是 SEASON/SEASON_SERIES/SERIES，
        # 也必须纳入级联同步——否则"重复绑定同一许可证"时子节点永远无法补齐缺失关联
        # （bug：导入的子内容未自动关联总季绑定的许可证）。
        content = (
            await db.execute(
                select(Content).where(Content.id == cid, Content.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if not content:
            raise BusinessException(ErrorCode.CONTENT_ID_NOT_FOUND, get_msg("CONTENT_ID_NOT_FOUND", cid=cid))

        if content.content_type == ContentType.SEASON.value:
            season_content_ids.append(cid)
        elif content.content_type == ContentType.SEASON_SERIES.value:
            season_series_content_ids.append(cid)
        elif content.content_type == ContentType.SERIES.value:
            series_content_ids.append(cid)

        if cid in existing_ids:
            continue
        if cid in soft_deleted_map:
            soft_deleted_map[cid].is_deleted = False
        else:
            db.add(LicenseContent(license_id=license_id, content_id=cid))
        existing_ids.add(cid)
        newly_linked_ids.append(cid)

    # ── 级联同步：SEASON / SEASON_SERIES / SERIES 操作时，子节点全量同步为父节点的许可证集合 ──
    # 级联同步中发生实际变更的子节点：(child_id, "add"|"remove", 变更的 license_id 集合)，
    # 用于同步完成后补写操作日志（无实际变更的子节点不写，保证幂等）
    cascade_changes: list[tuple[int, str, set[int]]] = []
    parent_ids_for_sync: list[int] = list(set(season_content_ids + season_series_content_ids + series_content_ids))
    if parent_ids_for_sync:
        for parent_id in parent_ids_for_sync:
            # 1. 获取父节点操作后的完整 license_id 集合
            parent_license_ids: set[int] = set(
                (
                    await db.execute(
                        select(LicenseContent.license_id).where(
                            LicenseContent.content_id == parent_id,
                            LicenseContent.is_deleted.is_(False),
                        )
                    )
                ).scalars().all()
            )

            # 2. 获取所有子节点 id
            if parent_id in season_content_ids:
                child_ids = await _get_season_descendant_ids(db, parent_id)
                parent_type = "SEASON"
            elif parent_id in season_series_content_ids:
                child_ids = await _get_season_series_child_ids(db, parent_id)
                parent_type = "SEASON_SERIES"
            else:
                child_ids = await _get_series_child_ids(db, parent_id)
                parent_type = "SERIES"

            if not child_ids:
                continue

            logger.info(
                f"[级联同步] {parent_type} #{parent_id} 同步 {len(child_ids)} 个子节点，"
                f"目标许可证集合: {parent_license_ids}"
            )

            # 3. 批量查询所有子节点当前的 (content_id, license_id) 关联
            child_license_rows = (
                await db.execute(
                    select(
                        LicenseContent.content_id,
                        LicenseContent.license_id,
                    ).where(
                        LicenseContent.content_id.in_(child_ids),
                        LicenseContent.is_deleted.is_(False),
                    )
                )
            ).all()
            child_license_map: dict[int, set[int]] = {}
            for cr in child_license_rows:
                child_license_map.setdefault(cr.content_id, set()).add(cr.license_id)

            # 4. 逐个子节点同步
            for child_id in child_ids:
                child_current = child_license_map.get(child_id, set())

                # 需要删除的：子节点有但父节点没有的
                extra_ids = child_current - parent_license_ids
                if extra_ids:
                    await db.execute(
                        update(LicenseContent)
                        .where(
                            LicenseContent.content_id == child_id,
                            LicenseContent.license_id.in_(extra_ids),
                            LicenseContent.is_deleted.is_(False),
                        )
                        .values(is_deleted=True)
                    )
                    cascade_changes.append((child_id, "remove", extra_ids))
                    logger.info(
                        f"[级联同步] 子节点 #{child_id} 移除多余许可证: {extra_ids}"
                    )

                # 需要添加的：父节点有但子节点没有的
                missing_ids = parent_license_ids - child_current
                if missing_ids:
                    # 检查是否存在已软删除的记录（恢复用）
                    soft_rows = (
                        await db.execute(
                            select(LicenseContent).where(
                                LicenseContent.content_id == child_id,
                                LicenseContent.license_id.in_(missing_ids),
                                LicenseContent.is_deleted.is_(True),
                            )
                        )
                    ).scalars().all()
                    soft_map = {r.license_id: r for r in soft_rows}

                    for missing_lid in missing_ids:
                        if missing_lid in soft_map:
                            soft_map[missing_lid].is_deleted = False
                        else:
                            db.add(LicenseContent(license_id=missing_lid, content_id=child_id))
                    cascade_changes.append((child_id, "add", missing_ids))
                    logger.info(
                        f"[级联同步] 子节点 #{child_id} 补充缺失许可证: {missing_ids}"
                    )

    # ── 级联同步补写操作日志 ──
    # 级联同步虽是数据一致性传播，但子节点的许可证集合实际发生了变化，
    # 需为每个发生实际变更的子节点写日志（content_id=子节点id），
    # 保证子节点（单季/单集）详情页 Activity Log 可见许可证变化
    if cascade_changes:
        license_obj = await _get_license_or_404(db, license_id)
        log_user_id = current_user.id if current_user else None
        log_user_name = current_user.username if current_user else "system"
        for child_id, action, changed_ids in cascade_changes:
            is_add = action == "add"
            raw_val = json.dumps({
                "license_id": license_id,
                "license_name": license_obj.name,
                "cascade": True,
                "changed_license_ids": sorted(changed_ids),
            }, ensure_ascii=False)
            snapshot_val = json.dumps({"license_name": license_obj.name}, ensure_ascii=False)
            await write_log(
                db,
                user_id=log_user_id,
                user_name=log_user_name,
                operation_type=OperationType.LICENSE_CONTENT_ADD if is_add else OperationType.LICENSE_CONTENT_REMOVE,
                operation_object_code="log.license.link" if is_add else "log.license.unlink",
                operation_content_code="log.license.link" if is_add else "log.license.unlink",
                content_id=child_id,
                entity_type="license",
                entity_id=license_id,
                previous_value=snapshot_val if not is_add else None,
                updated_value=snapshot_val if is_add else None,
                updated_value_json=raw_val,
                result="success",
            )

    # ── 状态回退：改谁的许可证就回退谁（仅自身，不级联子类） ──
    # 级联同步只是数据一致性传播（父级许可证集合同步给子节点），
    # 不视为对子节点的编辑，故只回退被直接操作的内容。
    if newly_linked_ids:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
        edited_by = current_user.username if current_user else "system"
        for cid in newly_linked_ids:
            content = await get_content_by_id(db, cid)
            if content:
                logger.info(
                    f"[许可证关联] 内容 #{cid} 新增许可证 {license_id}，回退自身状态（不影响子类）"
                )
                await rollback_after_published_edit(
                    db, cid, content.content_type, edited_by, "关联许可证"
                )

    await db.commit()
    return await list_license_contents(db, license_id, current_user)


async def remove_content_from_license(
    db: AsyncSession, license_id: int, content_id: int, processed_by: str | None = None,
    current_user: User | None = None,
) -> None:
    """
    从许可证移除内容关联（软删除）。

    输入参数：
        license_id      许可证 id
        content_id      要移除的内容 id
        current_user    当前登录用户（用于级联日志的操作人）

    业务规则：
        - 若移除的是总季（SEASON），同时级联移除其下所有单季和单集的同许可证关联
        - 若移除的是单季（SEASON_SERIES），同时级联移除其下所有单集的同许可证关联
        - 若移除的是普通连续剧（SERIES），同时级联移除其下所有单集的同许可证关联
        - 单集独立移除不影响父级
        - 状态回退仅作用于被直接操作的内容自身，不级联子类
        - 级联移除的子节点补写操作日志（content_id=子节点id），保证子节点详情页 Activity Log 可见
    """
    logger.info(f"remove_content_from_license 入参: license_id={license_id}, content_id={content_id}")
    await _get_license_or_404(db, license_id)

    # 先查出被移除内容的类型，判断是否需要级联
    content = (
        await db.execute(
            select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
        )
    ).scalar_one_or_none()

    # 移除自身的 LicenseContent 关联
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

    # ── 级联移除 ──
    if content:
        if content.content_type == ContentType.SEASON.value:
            descendant_ids = await _get_season_descendant_ids(db, content_id)
        elif content.content_type == ContentType.SEASON_SERIES.value:
            descendant_ids = await _get_season_series_child_ids(db, content_id)
        elif content.content_type == ContentType.SERIES.value:
            descendant_ids = await _get_series_child_ids(db, content_id)
        else:
            descendant_ids = []

        if descendant_ids:
            descendant_rows = (
                await db.execute(
                    select(LicenseContent).where(
                        LicenseContent.license_id == license_id,
                        LicenseContent.content_id.in_(descendant_ids),
                        LicenseContent.is_deleted.is_(False),
                    )
                )
            ).scalars().all()
            for descendant_row in descendant_rows:
                descendant_row.is_deleted = True
            logger.info(
                f"[级联许可证] {content.content_type} #{content_id} 移除许可证 {license_id}，"
                f"级联移除 {len(descendant_rows)} 个子节点的关联"
            )

            # 级联移除的子节点补写操作日志（content_id=子节点id），
            # 保证子节点（单季/单集）详情页 Activity Log 可见许可证移除
            license_obj = await _get_license_or_404(db, license_id)
            log_user_id = current_user.id if current_user else None
            log_user_name = current_user.username if current_user else (processed_by or "system")
            snapshot_val = json.dumps({"license_name": license_obj.name}, ensure_ascii=False)
            for descendant_row in descendant_rows:
                raw_val = json.dumps({
                    "license_id": license_id,
                    "license_name": license_obj.name,
                    "cascade": True,
                    "cascade_from": content_id,
                }, ensure_ascii=False)
                await write_log(
                    db,
                    user_id=log_user_id,
                    user_name=log_user_name,
                    operation_type=OperationType.LICENSE_CONTENT_REMOVE,
                    operation_object_code="log.license.unlink",
                    operation_content_code="log.license.unlink",
                    content_id=descendant_row.content_id,
                    entity_type="license",
                    entity_id=license_id,
                    previous_value=snapshot_val,
                    updated_value_json=raw_val,
                    result="success",
                )

    # ── 状态回退：改谁的许可证就回退谁（仅自身，不级联子类） ──
    # 级联移除只是数据一致性传播，不视为对子节点的编辑。
    if row and content:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        logger.info(
            f"[许可证关联] 内容 #{content_id} 移除许可证 {license_id}，回退自身状态（不影响子类）"
        )
        await rollback_after_published_edit(
            db, content_id, content.content_type,
            processed_by or "system", "移除许可证"
        )

    await db.commit()


async def list_available_contents(
    db: AsyncSession,
    license_id: int,
    page: int = 1,
    page_size: int = 10,
    title: str | None = None,
    content_types: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
    genre_ids: list[int] | None = None,
    without_license: bool = False,
    current_user: User | None = None,
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
        current_user    当前登录用户（用于数据权限过滤）

    输出：
        PaginatedResponse[ContentForTradeItem]
    """
    logger.info(f"list_available_contents 入参: license_id={license_id}, page={page}, page_size={page_size}, title={title}, content_types={content_types}, ingest_statuses={ingest_statuses}, genre_ids={genre_ids}, without_license={without_license}")
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

    query = select(Content).where(Content.is_deleted.is_(False), Content.is_discarded.is_(False))

    query = await apply_content_data_auth(db, current_user, query)

    if already_linked:
        query = query.where(Content.id.notin_(already_linked))
    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if ingest_statuses:
        query = query.where(Content.status.in_(ingest_statuses))
    if genre_ids:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id.in_(genre_ids))
        query = query.where(Content.id.in_(subq))
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
            genre=await _get_genre_names(db, c.id),
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
    genre_ids: list[int] | None = None,
    current_user: User | None = None,
) -> PaginatedResponse[ContentForTradeItem]:
    """
    查询未关联任何许可证的内容列表（用于"添加内容至合同"弹框左栏初始状态）。

    输入参数：
        page            页码
        page_size       每页条数
        title           内容标题关键字（模糊匹配）
        content_types   内容类型过滤列表
        ingest_statuses Ingest 状态过滤列表
        genre_ids       题材 ID 过滤
        current_user    当前登录用户（用于数据权限过滤）

    输出：
        PaginatedResponse[ContentForTradeItem]
    """
    logger.info(f"list_unlicensed_contents 入参: page={page}, page_size={page_size}, title={title}, content_types={content_types}, ingest_statuses={ingest_statuses}, genre_ids={genre_ids}")
    query = select(Content).where(Content.is_deleted.is_(False), Content.is_discarded.is_(False))

    query = await apply_content_data_auth(db, current_user, query)

    if title:
        query = query.where(Content.title.ilike(f"%{title}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if ingest_statuses:
        query = query.where(Content.status.in_(ingest_statuses))
    if genre_ids:
        subq = select(ContentGenre.content_id).where(ContentGenre.genre_id.in_(genre_ids))
        query = query.where(Content.id.in_(subq))
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
            genre=await _get_genre_names(db, c.id),
            original_name=None,
            release_year=None,
        ))

    return PaginatedResponse(total=total, page=page, page_size=page_size, items=items)


async def get_without_license_content_count(
    db: AsyncSession,
    current_user: User | None = None,
) -> int:
    """
    统计当前未关联任何许可证的内容数量。

    输入参数：
        current_user    当前登录用户（用于数据权限过滤）

    输出：
        整数，无许可证内容数量
    """
    logger.info(f"get_without_license_content_count 入参: 无")
    sub_lc = select(LicenseContent.content_id)
    query = select(Content).where(
        Content.is_deleted.is_(False),
        Content.id.notin_(sub_lc),
    )
    query = await apply_content_data_auth(db, current_user, query)
    count_query = select(func.count()).select_from(query.subquery())
    result = await db.execute(count_query)
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
