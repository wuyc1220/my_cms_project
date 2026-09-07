"""
C2 规范数据加载器。

以 ``content_id`` 为入口，聚合加载生成 ADI XML 所需的全部业务数据。

输出一个 :class:`BuildContext` 对象，承载：
- 主 Content + Metadata
- 关联 Category / Package / CustomTag
- 关联 Movie / CastRoleMap + Cast
- 关联 Picture
- 子 Content（EPISODE 的父 SERIES、CHANNEL 的 PhysicalChannel / Schedule）
- 许可证聚合数据（LicensingWindowStart/End, ContentTier, Provider 等）
- 自定义字段（EntityFieldValue）
- 多语言（EntityI18n）— 含回退逻辑
- Genre 名称
- ContentType 名称

多语言回退规则：
    第一种语言（Multi_Languages 排序首项）从模型字段取值；
    其他语言从 EntityI18n 获取，若某语言无 i18n 记录则回退复制第一种语言的值。

所有加载函数都是纯读，不修改任何数据库状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone

from sqlalchemy import and_, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .constants import ElementType

from app.internal.cms_biz_metada.models.basic import (
    Cast,
    Category,
    ContentType,
    CustomField,
    CustomTag,
    EntityFieldValue,
    EntityI18n,
    Genre,
    Picture,
    PosterSize,
    Tag,
)
from app.internal.cms_biz_orchestration.models.cast_role_map import CastRoleMap
from app.internal.cms_biz_orchestration.models.content_metadata import (
    ChannelMetadata,
    ContentMetadata,
    ScheduleMetadata,
    SeriesMetadata,
)
from app.internal.cms_biz_orchestration.models.movie import Movie
from app.internal.cms_biz_package.models.package import (
    Content,
    ContentCategory,
    ContentCustomTag,
    ContentGenre,
    ContentPackage,
    Package,
    PhysicalChannel,
)
from app.internal.cms_biz_scp.models.trade import (
    Contract,
    License,
    LicenseContent,
    LicensePlatform,
    Provider,
)
from app.internal.cms_biz_system.models.dict import DictNode


# ═══════════════════════════════════════════════════════════
# BuildContext — XML 生成上下文容器
# ═══════════════════════════════════════════════════════════
@dataclass
class BuildContext:
    """
    XML 生成上下文。

    承载从数据库一次性加载的所有业务数据，供 objects/mappings 模块消费。
    不在 builder 之外改动其内容。
    """

    # 主体 Content
    content: Content

    # 与 content_type 对应的元数据（四选一）
    program_meta: ContentMetadata | None = None
    series_meta: SeriesMetadata | None = None
    channel_meta: ChannelMetadata | None = None
    schedule_meta: ScheduleMetadata | None = None

    # 关联对象
    categories: list[tuple[Category, int]] = field(default_factory=list)  # (Category, sequence)
    packages: list[Package] = field(default_factory=list)
    movies: list[Movie] = field(default_factory=list)
    cast_role_maps: list[CastRoleMap] = field(default_factory=list)
    casts: list[Cast] = field(default_factory=list)
    pictures: list[Picture] = field(default_factory=list)

    # Channel 专用：关联的 PhysicalChannel
    physical_channels: list[PhysicalChannel] = field(default_factory=list)

    # SERIES/SEASON_SERIES/SEASON 专用：子 Content 列表（EPISODE 或子 SERIES）
    children: list[Content] = field(default_factory=list)

    # 许可证聚合数据
    license_data: dict = field(default_factory=dict)

    # Genre 名称（旧：单值，保留兼容）
    genre_name: str | None = None

    # Genre 多语言名称 {language: name}
    genre_i18n: dict = field(default_factory=dict)

    # ContentType 名称
    content_type_name: str | None = None

    # Provider Code（Channel 的 Cpcode）
    provider_code: str | None = None

    # 自定义字段（主 Content 类型：Series/Program/Channel/Schedule）
    # {"extendinfo": json_str} 或 {}（无自定义字段时为空字典）
    custom_fields: dict = field(default_factory=dict)

    # 各关联实体类型的自定义字段 {entity_id: {"extendinfo": json_str}}
    # 无自定义字段值的实体不包含 extendinfo key
    category_custom_fields: dict = field(default_factory=dict)
    package_custom_fields: dict = field(default_factory=dict)
    cast_custom_fields: dict = field(default_factory=dict)
    cast_role_map_custom_fields: dict = field(default_factory=dict)
    physical_channel_custom_fields: dict = field(default_factory=dict)
    movie_custom_fields: dict = field(default_factory=dict)
    # Picture 自定义字段 {poster_size_id: {"extendinfo": json_str}}
    # Picture 的扩展字段挂在其所属的海报规格(PosterSize)上
    picture_custom_fields: dict = field(default_factory=dict)

    # Tags 名称（tag_ids → Tag.name，按主语言查询，逗号分隔）
    tag_names: str | None = None

    # Tags 多语言 {language: "tag1,tag2"}
    tag_i18n: dict = field(default_factory=dict)

    # Cast 多语言数据 {cast_id: {field_name: {language: value}}}
    cast_i18n_data: dict = field(default_factory=dict)

    # Category 多语言数据 {category_id: {field_name: {language: value}}}
    category_i18n_data: dict = field(default_factory=dict)

    # Schedule Actor/Director 聚合 {language: "actor1,actor2"} / {language: "dir1,dir2"}
    schedule_actor_i18n: dict = field(default_factory=dict)
    schedule_director_i18n: dict = field(default_factory=dict)

    # Schedule PPV PackageID（逗号分隔）
    schedule_package_ids: str | None = None

    # Schedule Series 段 PackageID（连续剧订阅包，逗号分隔）
    series_package_ids: str | None = None

    # Schedule Series 段 VolumnCount（关联 Series 的实时集数；无关联 Series 时为 None）
    series_children_count: int | None = None

    # 所有支持的语言代码列表（按 Multi_Languages 排序，第一种为主语言）
    languages: list[str] = field(default_factory=list)

    # 主语言代码（languages[0]）
    primary_language: str = "en"

    # Content 多语言数据 {field_name: {language: value}}
    # 已含回退：其他语言无值时自动复制主语言值
    i18n_data: dict = field(default_factory=dict)

    # Movie 多语言数据 {movie_id: {field_name: {language: value}}}
    movie_i18n_data: dict = field(default_factory=dict)

    # PosterSize 映射 {picture_id: poster_size_name}
    poster_size_names: dict = field(default_factory=dict)

    # PosterSize mapping_type 映射 {picture_id: mapping_type}
    poster_size_mapping_types: dict = field(default_factory=dict)

    # Channel 的 channel_type（给 PhysicalChannel 用）
    channel_type: str | None = None

    # Schedule 的 ChannelID / ChannelCode
    schedule_channel_id: str | None = None
    schedule_channel_code: str | None = None


# ═══════════════════════════════════════════════════════════
# 加载主入口
# ═══════════════════════════════════════════════════════════
async def load_build_context(db: AsyncSession, content_id: int) -> BuildContext | None:
    """
    以 content_id 为入口聚合加载所有相关业务数据。

    :param db: 异步数据库会话
    :param content_id: 主 Content ID
    :return: 聚合完成的 :class:`BuildContext`，Content 不存在时返回 None
    """
    content = await _get_content(db, content_id)
    if content is None:
        return None

    ctx = BuildContext(content=content)

    # ── 1. 按 content_type 加载对应元数据 ──────────────────
    ct = content.content_type
    if ct in ("MOVIE", "EPISODE"):
        ctx.program_meta = await _get_one(db, ContentMetadata, ContentMetadata.content_id == content_id)
    elif ct in ("SERIES", "SEASON_SERIES", "SEASON"):
        ctx.series_meta = await _get_one(db, SeriesMetadata, SeriesMetadata.content_id == content_id)
    elif ct == "CHANNEL":
        ctx.channel_meta = await _get_one(db, ChannelMetadata, ChannelMetadata.content_id == content_id)
    elif ct == "SCHEDULE":
        ctx.schedule_meta = await _get_one(db, ScheduleMetadata, ScheduleMetadata.content_id == content_id)

    # ── 2. 通用关联：Category / Package ────────────────────
    ctx.categories = await _get_content_categories(db, content_id)
    ctx.packages = await _get_content_packages(db, content_id)

    # ── 3. 通用关联：Picture ──────────────────────────────
    ctx.pictures = await _get_pictures_for_entity(db, _picture_entity_type(ct), content_id)

    # ── 4. 类型专属加载 ────────────────────────────────────
    if ct in ("MOVIE", "EPISODE"):
        ctx.movies = await _get_movies(db, content_id)
        ctx.cast_role_maps = await _get_cast_role_maps(db, content_id)
        ctx.casts = await _get_casts_of_role_maps(db, ctx.cast_role_maps)

    elif ct in ("SERIES", "SEASON_SERIES", "SEASON"):
        ctx.children = await _get_children(db, content_id)
        ctx.cast_role_maps = await _get_cast_role_maps(db, content_id)
        ctx.casts = await _get_casts_of_role_maps(db, ctx.cast_role_maps)
        ctx.movies = await _get_movies(db, content_id)

    elif ct == "CHANNEL":
        ctx.physical_channels = await _get_physical_channels(db, content_id)
        ctx.children = await _get_children(db, content_id)
        if ctx.channel_meta:
            ctx.channel_type = ctx.channel_meta.channel_type

    elif ct == "SCHEDULE":
        ctx.cast_role_maps = await _get_cast_role_maps(db, content_id)
        ctx.casts = await _get_casts_of_role_maps(db, ctx.cast_role_maps)
        if content.parent_id:
            parent_channel = await _get_content(db, content.parent_id)
            if parent_channel:
                ctx.schedule_channel_id = str(parent_channel.id)
                ctx.schedule_channel_code = str(parent_channel.id)
                parent_ch_meta = await _get_one(
                    db, ChannelMetadata, ChannelMetadata.content_id == parent_channel.id
                )
                if parent_ch_meta:
                    ctx.channel_type = parent_ch_meta.channel_type

    # ── 4.5 补充加载关联对象的海报 ──────────────────────────
    if ctx.categories:
        cat_pics = await _get_pictures_for_entities(
            db, "category", [c.id for c, _ in ctx.categories]
        )
        ctx.pictures.extend(cat_pics)
    if ctx.casts:
        cast_pics = await _get_pictures_for_entities(
            db, "cast", [c.id for c in ctx.casts]
        )
        ctx.pictures.extend(cast_pics)

    # ── 5. 许可证聚合数据 ─────────────────────────────────
    ctx.license_data = await _load_license_data(db, content_id)

    # ── 6. 多语言（含语言列表 + 回退逻辑，提前加载供后续使用） ──
    ctx.languages, ctx.primary_language = await _load_languages(db)
    ctx.i18n_data = await _load_content_i18n(db, content_id, ctx.languages, ctx.primary_language)

    # ── 7. Genre 名称 + 多语言 ──────────────────────────
    ctx.genre_name = await _load_genre_name(db, content)
    ctx.genre_i18n = await _load_genre_i18n(db, content, ctx.languages)

    # ── 8. ContentType 名称 ───────────────────────────────
    ctx.content_type_name = await _load_content_type_name(db, content)

    # ── 9. Provider Code ──────────────────────────────────
    ctx.provider_code = await _load_provider_code(db, content_id)

    # ── 10. 自定义字段 ────────────────────────────────────
    ctx.custom_fields = await _load_custom_fields(db, content_id, ctx.languages, ctx.primary_language)

    # ── 10A. 各关联实体类型的自定义字段 ─────────────────────
    if ctx.categories:
        ctx.category_custom_fields = await _load_custom_fields_for_entities(
            db, "category", [c.id for c, _ in ctx.categories], ctx.languages, ctx.primary_language
        )
    if ctx.packages:
        ctx.package_custom_fields = await _load_custom_fields_for_entities(
            db, "package", [p.id for p in ctx.packages], ctx.languages, ctx.primary_language
        )
    if ctx.casts:
        ctx.cast_custom_fields = await _load_custom_fields_for_entities(
            db, "cast", [c.id for c in ctx.casts], ctx.languages, ctx.primary_language
        )
    if ctx.cast_role_maps:
        ctx.cast_role_map_custom_fields = await _load_custom_fields_for_entities(
            db, "cast_role_map", [r.map_id for r in ctx.cast_role_maps], ctx.languages, ctx.primary_language
        )
    if ctx.physical_channels:
        ctx.physical_channel_custom_fields = await _load_custom_fields_for_entities(
            db, "PhysicalChannel", [p.id for p in ctx.physical_channels], ctx.languages, ctx.primary_language
        )
    if ctx.movies:
        ctx.movie_custom_fields = await _load_custom_fields_for_entities(
            db, "movie", [m.id for m in ctx.movies], ctx.languages, ctx.primary_language
        )
    # Picture 的自定义字段按其所属海报规格加载（entity_type="poster_size"）
    if ctx.pictures:
        ctx.picture_custom_fields = await _load_custom_fields_for_entities(
            db, "poster_size",
            list({p.poster_size_id for p in ctx.pictures if p.poster_size_id}),
            ctx.languages, ctx.primary_language,
        )

    # ── 10.2 Tags 名称（tag_ids → Tag.name）─────────────
    ctx.tag_names, ctx.tag_i18n = await _load_tag_names(db, content, ctx.languages)

    # ── 11. PosterSize 名称映射和 mapping_type ────────────
    ctx.poster_size_names, ctx.poster_size_mapping_types = await _load_poster_size_names(db, ctx.pictures)

    # ── 12. Movie 多语言数据 ─────────────────────────────
    if ctx.movies:
        ctx.movie_i18n_data = await _load_movie_i18n_data(db, ctx.movies, ctx.languages, ctx.primary_language)

    # ── 13. Cast 多语言数据 ──────────────────────────────
    if ctx.casts:
        ctx.cast_i18n_data = await _load_cast_i18n_data(db, ctx.casts, ctx.languages, ctx.primary_language)

    # ── 14. Category 多语言数据 ─────────────────────────
    if ctx.categories:
        ctx.category_i18n_data = await _load_category_i18n_data(db, [c for c, _ in ctx.categories], ctx.languages, ctx.primary_language)

    # ── 15. Schedule Actor/Director 聚合 + PackageID ───
    if ct == "SCHEDULE":
        if ctx.casts:
            ctx.schedule_actor_i18n, ctx.schedule_director_i18n = await _load_schedule_actor_director(
                db, ctx.casts, ctx.cast_role_maps, ctx.languages, ctx.primary_language
            )
        # PackageID 加载不应该依赖 casts
        ctx.schedule_package_ids = await _load_schedule_package_ids(db, ctx.schedule_meta)
        ctx.series_package_ids = await _load_series_package_ids(db, ctx.schedule_meta)
        ctx.series_children_count = await _load_series_children_count(db, ctx.schedule_meta)

    return ctx


# ═══════════════════════════════════════════════════════════
# 私有加载函数
# ═══════════════════════════════════════════════════════════
async def _get_content(db: AsyncSession, content_id: int) -> Content | None:
    """查询单条 Content（过滤软删除和作废）"""
    stmt = select(Content).where(
        and_(
            Content.id == content_id,
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),
        )
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _get_one(db: AsyncSession, model, where_expr):
    """通用单条查询（过滤软删除和废弃标识，取最新一条）"""
    stmt = select(model).where(where_expr, model.is_deleted.is_(False))
    if hasattr(model, 'is_discarded'):
        stmt = stmt.where(model.is_discarded.is_(False))
    stmt = stmt.order_by(desc(model.id))
    return (await db.execute(stmt)).scalars().first()


async def _get_content_categories(db: AsyncSession, content_id: int) -> list[tuple[Category, int]]:
    """查询 Content 关联的所有 Category，返回 (Category, sequence) 元组列表"""
    stmt = (
        select(Category, ContentCategory.sequence)
        .join(ContentCategory, ContentCategory.category_id == Category.id)
        .where(
            ContentCategory.content_id == content_id,
            ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False),
            Category.is_deleted.is_(False),
        )
        .order_by(ContentCategory.sequence, Category.id)
    )
    rows = (await db.execute(stmt)).all()
    return [(row.Category, row.sequence) for row in rows]


async def _get_content_packages(db: AsyncSession, content_id: int) -> list[Package]:
    """查询 Content 关联的所有 Package"""
    stmt = (
        select(Package)
        .join(ContentPackage, ContentPackage.package_id == Package.id)
        .where(
            ContentPackage.content_id == content_id,
            ContentPackage.is_deleted.is_(False), ContentPackage.is_discarded.is_(False),
            Package.is_deleted.is_(False),
        )
    )
    return list((await db.execute(stmt)).scalars().all())


async def _get_movies(db: AsyncSession, content_id: int) -> list[Movie]:
    """查询 Content 关联的所有 Movie（含正片/预告片/字幕）"""
    stmt = select(Movie).where(
        Movie.content_id == content_id, Movie.is_deleted.is_(False), Movie.is_discarded.is_(False)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _get_cast_role_maps(db: AsyncSession, content_id: int) -> list[CastRoleMap]:
    """查询 Content 关联的所有 CastRoleMap"""
    stmt = select(CastRoleMap).where(
        CastRoleMap.content_id == content_id,
        CastRoleMap.is_deleted.is_(False), CastRoleMap.is_discarded.is_(False),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _get_casts_of_role_maps(
    db: AsyncSession, role_maps: list[CastRoleMap]
) -> list[Cast]:
    """根据 CastRoleMap 批量加载对应的 Cast（去重）"""
    cast_ids = list({rm.cast_id for rm in role_maps if rm.cast_id})
    if not cast_ids:
        return []
    stmt = select(Cast).where(Cast.id.in_(cast_ids), Cast.is_deleted.is_(False))
    return list((await db.execute(stmt)).scalars().all())


async def _get_pictures_for_entity(
    db: AsyncSession, entity_type: str, entity_id: int
) -> list[Picture]:
    """按多态键查询关联图片"""
    if not entity_type:
        return []
    stmt = select(Picture).where(
        Picture.entity_type == entity_type,
        Picture.entity_id == entity_id,
        Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _get_pictures_for_entities(
    db: AsyncSession, entity_type: str, entity_ids: list[int]
) -> list[Picture]:
    """批量查询同类型多实体的关联图片（去重）"""
    if not entity_type or not entity_ids:
        return []
    seen = set()
    result = []
    stmt = select(Picture).where(
        Picture.entity_type == entity_type,
        Picture.entity_id.in_(entity_ids),
        Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    for pic in rows:
        if pic.id not in seen:
            seen.add(pic.id)
            result.append(pic)
    return result


async def _get_physical_channels(db: AsyncSession, channel_content_id: int) -> list[PhysicalChannel]:
    """查询业务频道下属的所有物理频道"""
    stmt = select(PhysicalChannel).where(
        PhysicalChannel.channel_id == channel_content_id,
        PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _get_children(db: AsyncSession, parent_id: int) -> list[Content]:
    """查询父 Content 的直接子 Content（EPISODE/SERIES/SCHEDULE）"""
    stmt = select(Content).where(
        Content.parent_id == parent_id,
        Content.is_deleted.is_(False),
        Content.is_discarded.is_(False),
    ).order_by(Content.sequence)
    return list((await db.execute(stmt)).scalars().all())


def _picture_entity_type(content_type: str) -> str:
    """Content.content_type → Picture.entity_type 的反向映射（小写，与数据库一致）"""
    if content_type in ("MOVIE", "EPISODE"):
        return "program"
    if content_type in ("SERIES", "SEASON_SERIES", "SEASON"):
        return "series"
    if content_type == "CHANNEL":
        return "channel"
    if content_type == "SCHEDULE":
        return "schedule"
    return ""


def _to_utc_timestamp(d: date, is_end: bool = False) -> str:
    """将 date 转为 C2 规范要求的 UTC 时间戳格式 (YYYY-MM-DDTHH:MM:SSZ)。"""
    t = time(23, 59, 59) if is_end else time.min
    return datetime.combine(d, t, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ═══════════════════════════════════════════════════════════
# 许可证聚合加载
# ═══════════════════════════════════════════════════════════
async def _load_license_data(db: AsyncSession, content_id: int) -> dict:
    """
    从许可证表聚合计算 C2 规范需要的许可证相关字段。

    逻辑（参考元数据.md）：
    - LicensingWindowStart = 所有许可证 start_date 的最小值
    - LicensingWindowEnd = 所有许可证 end_date 的最大值
    - ContentTier = 所有许可证平台 platform 的合集（逗号分隔）
    - Provider = 许可证→合同→供应商 name
    - Downloadable = 任一许可证 mobile_download=True 则为 1
    - LicenseDuration = 第一个有 download_duration 的许可证值
    - Previewable = 任一许可证 mobile_preview=True 则为 1
    - AdvertorialRights = 所有许可证平台 ad_rights=True 的 platform 合集
    """
    stmt = (
        select(License.id)
        .join(LicenseContent, LicenseContent.license_id == License.id)
        .where(
            LicenseContent.content_id == content_id,
            LicenseContent.is_deleted.is_(False),
            License.is_deleted.is_(False),
            License.status == "ACTIVE",
        )
    )
    license_ids = list((await db.execute(stmt)).scalars().all())
    if not license_ids:
        return {}

    licenses = []
    for lid in license_ids:
        lic = await _get_one(db, License, License.id == lid)
        if lic:
            licenses.append(lic)

    if not licenses:
        return {}

    start_dates = [lic.start_date for lic in licenses if lic.start_date]
    end_dates = [lic.end_date for lic in licenses if lic.end_date]

    all_platforms = set()
    ad_platforms = set()
    for lid in license_ids:
        stmt_lp = select(LicensePlatform).where(
            LicensePlatform.license_id == lid,
            LicensePlatform.is_deleted.is_(False),
        )
        lps = list((await db.execute(stmt_lp)).scalars().all())
        for lp in lps:
            all_platforms.add(lp.platform)
            if lp.ad_rights:
                ad_platforms.add(lp.platform)

    provider_name = None
    for lic in licenses:
        if lic.contract_id:
            contract = await _get_one(db, Contract, Contract.id == lic.contract_id)
            if contract and contract.provider_id:
                provider = await _get_one(db, Provider, Provider.id == contract.provider_id)
                if provider:
                    provider_name = provider.name
                    break

    downloadable = 1 if any(lic.mobile_download for lic in licenses) else 0
    license_duration = next(
        (lic.download_duration for lic in licenses if lic.download_duration), None
    )
    previewable = 1 if any(lic.mobile_preview for lic in licenses) else 0

    preview_begin = None
    preview_end = None
    for lic in licenses:
        if lic.mobile_preview:
            if lic.preview_begin_time:
                preview_begin = lic.preview_begin_time.strftime("%H:%M:%S")
            if lic.preview_end_time:
                preview_end = lic.preview_end_time.strftime("%H:%M:%S")
            break

    result = {}
    if start_dates:
        result["licensing_window_start"] = _to_utc_timestamp(min(start_dates), is_end=False)
    if end_dates:
        result["licensing_window_end"] = _to_utc_timestamp(max(end_dates), is_end=True)
    if all_platforms:
        result["content_tier"] = str(sum(int(p) for p in all_platforms if p.isdigit() or (p.startswith("-") and p[1:].isdigit())))
    if provider_name:
        result["provider"] = provider_name
    result["downloadable"] = downloadable
    if license_duration is not None:
        result["license_duration"] = license_duration
    result["previewable"] = previewable
    if preview_begin:
        result["preview_begin_time"] = preview_begin
    if preview_end:
        result["preview_end_time"] = preview_end
    if ad_platforms:
        result["advertorial_rights"] = str(sum(int(p) for p in ad_platforms if p.isdigit() or (p.startswith("-") and p[1:].isdigit())))

    return result


# ═══════════════════════════════════════════════════════════
# Genre / ContentType / Provider 加载
# ═══════════════════════════════════════════════════════════
async def _load_genre_name(db: AsyncSession, content: Content) -> str | None:
    """从 content_genre 中间表查询第一个 Genre 名称（主语言）"""
    stmt = (
        select(Genre.name)
        .join(ContentGenre, ContentGenre.genre_id == Genre.id)
        .where(
            ContentGenre.content_id == content.id,
            ContentGenre.is_deleted.is_(False),
            Genre.is_deleted.is_(False),
        )
        .limit(1)
    )
    row = (await db.execute(stmt)).scalar()
    return row if row else None


async def _load_genre_i18n(db: AsyncSession, content: Content, languages: list[str]) -> dict:
    """
    查询多语言 genre_ids，主语言从 content 表获取，其他语言从 entity_i18n 表获取。

    逻辑：
    1. 主语言：从 content 表获取 genre_id
    2. 其他语言：从 entity_i18n 表获取 genre_ids（逗号分隔）
    3. 查询 Genre 表获取对应语言的类型名称
    4. 拼接多个类型名称，用逗号分隔

    返回 {language: "name1,name2"}，如 {"en": "Action,Comedy", "cn": "动作,喜剧"}。
    """
    from app.internal.cms_biz_metada.models.basic import EntityI18n

    if not languages:
        return {}

    primary_lang = languages[0]

    # 1. 从 entity_i18n 表获取所有语言的 genre_ids（包括主语言）
    lang_genre_ids: dict[str, list[int]] = {}

    i18n_stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "content",
        EntityI18n.entity_id == content.id,
        EntityI18n.field_name == "genre_ids",
        EntityI18n.is_deleted.is_(False),
    )
    i18n_rows = list((await db.execute(i18n_stmt)).scalars().all())

    for row in i18n_rows:
        if row.value:
            try:
                # genre_ids 是逗号分隔的列表
                genre_ids = [int(gid.strip()) for gid in str(row.value).split(",") if gid.strip()]
                if genre_ids:
                    lang_genre_ids[row.language] = genre_ids
            except (ValueError, TypeError):
                continue

    # 2. 如果 entity_i18n 中没有主语言的 genre_ids，则回退到 content_genre 中间表
    if primary_lang not in lang_genre_ids:
        fallback_stmt = select(ContentGenre.genre_id).where(
            ContentGenre.content_id == content.id,
            ContentGenre.is_deleted.is_(False),
        )
        fallback_ids = [r[0] for r in (await db.execute(fallback_stmt)).all()]
        if fallback_ids:
            lang_genre_ids[primary_lang] = fallback_ids

    if not lang_genre_ids:
        return {}

    # 3. 收集所有需要的 genre_id 去查询 Genre 表
    all_genre_ids = set()
    for genre_ids in lang_genre_ids.values():
        all_genre_ids.update(genre_ids)

    # 查询 Genre 表获取所有需要的类型信息
    genre_stmt = select(Genre).where(
        Genre.id.in_(all_genre_ids),
        Genre.is_deleted.is_(False),
    )
    genre_rows = list((await db.execute(genre_stmt)).scalars().all())

    # 构建 genre 查找表：{genre_id: {language: genre_name}}
    genre_lookup: dict[int, dict[str, str]] = {}
    for genre in genre_rows:
        if genre.id not in genre_lookup:
            genre_lookup[genre.id] = {}
        if genre.language and genre.name:
            genre_lookup[genre.id][genre.language] = genre.name

    # 4. 为每种语言构建类型名称列表（多个类型用逗号分隔）
    result: dict[str, str] = {}
    for lang in languages:
        # 获取该语言的 genre_ids，如果没有则使用主语言的
        genre_ids = lang_genre_ids.get(lang)
        if not genre_ids:
            genre_ids = lang_genre_ids.get(primary_lang, [])
        if not genre_ids:
            continue

        # 为每个 genre_id 获取对应语言的名称
        genre_names = []
        for gid in genre_ids:
            # 优先获取当前语言的类型名
            name = genre_lookup.get(gid, {}).get(lang)
            if not name:
                # 如果没有当前语言的，使用主语言的
                name = genre_lookup.get(gid, {}).get(primary_lang)
            if name:
                genre_names.append(name)

        if genre_names:
            result[lang] = ",".join(genre_names)

    return result


async def _load_tag_names(
    db: AsyncSession, content: Content, languages: list[str]
) -> tuple[str | None, dict]:
    """
    查询多语言 tag_ids，优先从 entity_i18n 获取，没有则使用主表值。

    逻辑：
    1. 从主表 (program_metadata/series_metadata) 获取主语言的 tag_ids
    2. 从 entity_i18n 表获取其他语言的 tag_ids
    3. 查询 Tag 表获取对应语言的标签名称

    返回 (主语言逗号分隔名称, {language: 逗号分隔名称})。
    """
    from app.internal.cms_biz_metada.models.basic import EntityI18n

    if not languages:
        return None, {}

    primary_lang = languages[0]

    # 1. 从主表获取主语言的 tag_ids
    meta = None
    if content.content_type in ("MOVIE", "EPISODE"):
        meta = await _get_one(db, ContentMetadata, ContentMetadata.content_id == content.id)
    elif content.content_type in ("SERIES", "SEASON_SERIES", "SEASON"):
        meta = await _get_one(db, SeriesMetadata, SeriesMetadata.content_id == content.id)
    elif content.content_type == "SCHEDULE":
        meta = await _get_one(db, ScheduleMetadata, ScheduleMetadata.content_id == content.id)

    # 解析 entity_i18n 数据：{language: "tag_id1,tag_id2"}
    lang_tag_ids: dict[str, list[int]] = {}
    
    # 先从主表获取主语言的值
    if meta and hasattr(meta, "tag_ids") and meta.tag_ids:
        try:
            primary_tag_ids = [int(tid) for tid in meta.tag_ids if isinstance(tid, int) or (isinstance(tid, str) and tid.isdigit())]
            lang_tag_ids[primary_lang] = primary_tag_ids
        except (ValueError, TypeError):
            pass

    # 2. 从 entity_i18n 表获取其他语言的 tag_ids
    i18n_stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "content",
        EntityI18n.entity_id == content.id,
        EntityI18n.field_name == "tag_ids",
        EntityI18n.is_deleted.is_(False),
    )
    i18n_rows = list((await db.execute(i18n_stmt)).scalars().all())

    for row in i18n_rows:
        if row.value and row.language != primary_lang:  # 跳过主语言，已处理
            try:
                tag_ids = [int(tid.strip()) for tid in str(row.value).split(",") if tid.strip()]
                lang_tag_ids[row.language] = tag_ids
            except (ValueError, TypeError):
                continue

    if not lang_tag_ids:
        return None, {}

    # 3. 查询 Tag 表获取标签名称
    all_tag_ids = set()
    for tag_ids in lang_tag_ids.values():
        all_tag_ids.update(tag_ids)

    tag_stmt = select(Tag).where(
        Tag.id.in_(all_tag_ids),
        Tag.is_deleted.is_(False),
    )
    tag_rows = list((await db.execute(tag_stmt)).scalars().all())

    # 构建 tag 查找表：{tag_id: {language: tag_name}}
    tag_lookup: dict[int, dict[str, str]] = {}
    for tag in tag_rows:
        if tag.id not in tag_lookup:
            tag_lookup[tag.id] = {}
        if tag.language and tag.name:
            tag_lookup[tag.id][tag.language] = tag.name

    # 4. 为每种语言构建标签名称列表
    result: dict[str, str] = {}
    for lang in languages:
        # 获取该语言的 tag_ids，如果没有则使用主语言的
        tag_ids = lang_tag_ids.get(lang)
        if not tag_ids:
            tag_ids = lang_tag_ids.get(primary_lang, [])
        if not tag_ids:
            continue

        # 获取标签名称
        tag_names = []
        for tid in tag_ids:
            # 优先获取当前语言的标签名
            name = tag_lookup.get(tid, {}).get(lang)
            if not name:
                # 如果没有当前语言的，使用主语言的
                name = tag_lookup.get(tid, {}).get(primary_lang)
            if name:
                tag_names.append(name)

        if tag_names:
            result[lang] = ",".join(tag_names)

    primary = result.get(primary_lang)
    return primary, result

async def _load_content_type_name(db: AsyncSession, content: Content) -> str | None:
    """从 content_type 映射到 C2 规范的 Type 值"""
    type_id = None
    meta = None
    if content.content_type in ("MOVIE", "EPISODE"):
        meta = await _get_one(db, ContentMetadata, ContentMetadata.content_id == content.id)
    elif content.content_type in ("SERIES", "SEASON_SERIES", "SEASON"):
        meta = await _get_one(db, SeriesMetadata, SeriesMetadata.content_id == content.id)
    if meta and hasattr(meta, "type_id"):
        type_id = meta.type_id
    if type_id is None:
        return content.content_type
    ct = await _get_one(db, ContentType, ContentType.id == type_id)
    return ct.name if ct else content.content_type


async def _load_provider_code(db: AsyncSession, content_id: int) -> str | None:
    """从许可证链路获取 Provider Code"""
    stmt = (
        select(License.contract_id)
        .join(LicenseContent, LicenseContent.license_id == License.id)
        .where(
            LicenseContent.content_id == content_id,
            LicenseContent.is_deleted.is_(False),
            License.is_deleted.is_(False),
            License.status == "ACTIVE",
        )
        .limit(1)
    )
    contract_id = (await db.execute(stmt)).scalar_one_or_none()
    if contract_id is None:
        return None
    contract = await _get_one(db, Contract, Contract.id == contract_id)
    if contract and contract.provider_id:
        provider = await _get_one(db, Provider, Provider.id == contract.provider_id)
        return provider.provider_code if provider else None
    return None


def _format_custom_field_value(value: str | None, field_type: str) -> str | None:
    """
    将自定义字段值按字段类型格式化为 C2 规范要求的格式。

    - Date+Time → UTC 格式 2019-09-11T13:23:18.55Z
    - Date → 保持 YYYY-MM-DD
    - Time → 保持 HH:MM:SS
    - 其他类型 → 原样返回
    """
    if value is None or value == "":
        return value
    if field_type == "Date+Time":
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            # 不转换时区，直接格式化为 UTC 字符串
            # 如果有 +08:00，保持原值；如果无时区，也保持原值
            return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{dt.microsecond // 1000:03d}Z"
        except (ValueError, TypeError):
            return value
    if field_type == "Date":
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return value
        except ValueError:
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return dt.strftime("%Y-%m-%d")
            except (ValueError, TypeError):
                return value
    if field_type == "Time":
        return value
    return value


# ═══════════════════════════════════════════════════════════
# 自定义字段加载
# ═══════════════════════════════════════════════════════════
async def _load_custom_fields(db: AsyncSession, content_id: int, languages: list[str], primary_language: str = "en") -> dict:
    """
    从 EntityFieldValue + EntityI18n 加载自定义字段。

    返回 {"extendinfo": json_str}，格式：
      非多语言字段 → {"field_name": "value"}
      多语言字段   → {"field_name": {"en": "val", "zh": "val"}}
    没有自定义字段时不包含 extendinfo key。
    """
    import json

    extendinfo_parts: dict = {}

    # 1. 从 EntityFieldValue 加载非多语言字段值
    stmt = select(EntityFieldValue).where(
        EntityFieldValue.entity_type == "Content",
        EntityFieldValue.entity_id == content_id,
        EntityFieldValue.is_deleted.is_(False),
    )
    values = list((await db.execute(stmt)).scalars().all())

    cf_ids = list({v.custom_field_id for v in values if v.custom_field_id})
    cf_rows = []
    if cf_ids:
        cf_rows = (await db.execute(
            select(CustomField).where(CustomField.id.in_(cf_ids))
        )).scalars().all()
    cf_map = {cf.id: cf for cf in cf_rows}

    # 多语言字段回退 {field_code: (field_name, field_type, value)}
    # 字段切换 multi_language 开关后，存量值可能仍留在另一张表中，读取时双向回退
    ml_fallback: dict[str, tuple[str, str, str]] = {}
    for ev in values:
        if ev.custom_field_id and ev.custom_field_id in cf_map:
            cf = cf_map[ev.custom_field_id]
            if not cf.multi_language:
                extendinfo_parts[cf.field_name] = _format_custom_field_value(ev.value, cf.field_type)
            elif ev.value:
                ml_fallback[cf.field_code] = (cf.field_name, cf.field_type, ev.value)

    # 2. 从 EntityI18n 加载多语言字段（所有语言）
    i18n_stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "content",
        EntityI18n.entity_id == content_id,
        EntityI18n.field_name.startswith("cf_"),
        EntityI18n.is_deleted.is_(False),
    )
    i18n_rows = list((await db.execute(i18n_stmt)).scalars().all())

    if i18n_rows:
        field_codes = list({r.field_name for r in i18n_rows})
        cf_i18n_rows = (await db.execute(
            select(CustomField).where(
                CustomField.field_code.in_(field_codes),
                CustomField.is_deleted.is_(False),
            )
        )).scalars().all()
        cf_name_map = {cf.field_code: cf.field_name for cf in cf_i18n_rows}
        cf_i18n_type_map = {cf.field_code: cf.field_type for cf in cf_i18n_rows}
        cf_i18n_ml_map = {cf.field_code: cf.multi_language for cf in cf_i18n_rows}

        field_lang_values: dict[str, dict[str, str]] = {}
        for row in i18n_rows:
            field_lang_values.setdefault(row.field_name, {})[row.language] = row.value

        for field_code, lang_vals in field_lang_values.items():
            display_name = cf_name_map.get(field_code)
            if not display_name:
                continue
            ft = cf_i18n_type_map.get(field_code, "")
            if not cf_i18n_ml_map.get(field_code, True):
                # 字段已切换为非多语言：EntityFieldValue 无值时回退使用存量多语言值（默认语言优先）
                if display_name not in extendinfo_parts:
                    val = next((lang_vals.get(lang) for lang in languages if lang_vals.get(lang)), None) \
                        or lang_vals.get(primary_language)
                    if val:
                        extendinfo_parts[display_name] = _format_custom_field_value(val, ft)
                continue
            lang_obj = {}
            for lang in languages:
                val = lang_vals.get(lang) or lang_vals.get(primary_language)
                if val:
                    lang_obj[lang] = _format_custom_field_value(val, ft)
            if lang_obj:
                extendinfo_parts[display_name] = lang_obj

    # 2.5 多语言字段回退：EntityI18n 无记录时，用 EntityFieldValue 中的值填充所有语言
    for field_code, (display_name, ft, val) in ml_fallback.items():
        if display_name in extendinfo_parts:
            continue
        formatted = _format_custom_field_value(val, ft)
        if formatted:
            extendinfo_parts[display_name] = {lang: formatted for lang in languages}

    # 3. 组装结果
    result = {}
    if extendinfo_parts:
        result["extendinfo"] = json.dumps(extendinfo_parts, ensure_ascii=False)

    return result


async def _load_custom_fields_for_entities(
    db: AsyncSession, entity_type: str, entity_ids: list[int],
    languages: list[str], primary_language: str = "en",
) -> dict[int, dict]:
    """
    批量加载某实体类型的自定义字段值。

    返回 {entity_id: {"extendinfo": json_str}}，格式：
      非多语言字段 → {"field_name": "value"}
      多语言字段   → {"field_name": {"en": "val", "zh": "val"}}
    没有自定义字段值的实体不包含 extendinfo key。
    """
    if not entity_ids:
        return {}

    import json
    from collections import defaultdict

    # 1. 从 EntityFieldValue 加载非多语言字段值
    stmt = select(EntityFieldValue).where(
        func.lower(EntityFieldValue.entity_type) == func.lower(entity_type),
        EntityFieldValue.entity_id.in_(entity_ids),
        EntityFieldValue.is_deleted.is_(False),
    )
    values = list((await db.execute(stmt)).scalars().all())

    cf_ids = list({v.custom_field_id for v in values if v.custom_field_id})
    cf_map: dict[int, CustomField] = {}
    if cf_ids:
        for cf in (await db.execute(select(CustomField).where(CustomField.id.in_(cf_ids)))).scalars().all():
            cf_map[cf.id] = cf

    field_parts: dict[int, dict] = defaultdict(dict)
    # 多语言字段回退 {entity_id: {field_code: (field_name, field_type, value)}}
    # 部分实体（如 poster_size）的多语言字段仅将主语言值存于 EntityFieldValue，无 EntityI18n 记录
    ml_fallback: dict[int, dict] = defaultdict(dict)
    for ev in values:
        if ev.custom_field_id and ev.custom_field_id in cf_map:
            cf = cf_map[ev.custom_field_id]
            if not cf.multi_language:
                field_parts[ev.entity_id][cf.field_name] = _format_custom_field_value(ev.value, cf.field_type)
            elif ev.value:
                ml_fallback[ev.entity_id][cf.field_code] = (cf.field_name, cf.field_type, ev.value)

    # 2. 从 EntityI18n 加载多语言字段（所有语言）
    i18n_stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == func.lower(entity_type),
        EntityI18n.entity_id.in_(entity_ids),
        EntityI18n.field_name.startswith("cf_"),
        EntityI18n.is_deleted.is_(False),
    )
    i18n_rows = list((await db.execute(i18n_stmt)).scalars().all())

    i18n_field_codes = list({r.field_name for r in i18n_rows})
    cf_i18n_name_map: dict[str, str] = {}
    cf_i18n_type_map: dict[str, str] = {}
    cf_i18n_ml_map: dict[str, bool] = {}
    if i18n_field_codes:
        for cf in (await db.execute(
            select(CustomField).where(
                CustomField.field_code.in_(i18n_field_codes),
                CustomField.is_deleted.is_(False),
            )
        )).scalars().all():
            cf_i18n_name_map[cf.field_code] = cf.field_name
            cf_i18n_type_map[cf.field_code] = cf.field_type
            cf_i18n_ml_map[cf.field_code] = cf.multi_language

    entity_i18n_values: dict[int, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for row in i18n_rows:
        display_name = cf_i18n_name_map.get(row.field_name)
        if display_name:
            entity_i18n_values[row.entity_id][row.field_name][row.language] = row.value

    for eid, field_lang_map in entity_i18n_values.items():
        for field_code, lang_vals in field_lang_map.items():
            display_name = cf_i18n_name_map.get(field_code)
            if not display_name:
                continue
            ft = cf_i18n_type_map.get(field_code, "")
            if not cf_i18n_ml_map.get(field_code, True):
                # 字段已切换为非多语言：EntityFieldValue 无值时回退使用存量多语言值（默认语言优先）
                if display_name not in field_parts[eid]:
                    val = next((lang_vals.get(lang) for lang in languages if lang_vals.get(lang)), None) \
                        or lang_vals.get(primary_language)
                    if val:
                        field_parts[eid][display_name] = _format_custom_field_value(val, ft)
                continue
            lang_obj = {}
            for lang in languages:
                val = lang_vals.get(lang) or lang_vals.get(primary_language)
                if val:
                    lang_obj[lang] = _format_custom_field_value(val, ft)
            if lang_obj:
                field_parts[eid][display_name] = lang_obj

    # 2.5 多语言字段回退：EntityI18n 无记录时，用 EntityFieldValue 中的值填充所有语言
    for eid, fields in ml_fallback.items():
        for field_code, (display_name, ft, val) in fields.items():
            if display_name in field_parts[eid]:
                continue
            formatted = _format_custom_field_value(val, ft)
            if formatted:
                field_parts[eid][display_name] = {lang: formatted for lang in languages}

    # 3. 组装结果
    result: dict[int, dict] = {}
    for eid in entity_ids:
        parts = field_parts.get(eid, {})
        if parts:
            result[eid] = {"extendinfo": json.dumps(parts, ensure_ascii=False)}

    return result


# ═══════════════════════════════════════════════════════════
# 多语言加载（含回退逻辑）
# ═══════════════════════════════════════════════════════════
async def _load_languages(db: AsyncSession) -> tuple[list[str], str]:
    """
    从 Multi_Languages 字典加载所有支持的语言代码列表。

    :return: (语言代码列表, 主语言代码)
    """
    root = (
        await db.execute(
            select(DictNode).where(
                DictNode.parent_id.is_(None),
                DictNode.code == "Multi_Languages",
                DictNode.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if root is None:
        return ["en"], "en"

    children = (
        await db.execute(
            select(DictNode)
            .where(
                DictNode.parent_id == root.id,
                DictNode.status == "active",
                DictNode.is_deleted.is_(False),
            )
            .order_by(DictNode.sort_order, DictNode.id)
        )
    ).scalars().all()

    if not children:
        return ["en"], "en"

    languages = [c.code for c in children]
    return languages, languages[0]


async def _load_content_i18n(
    db: AsyncSession,
    content_id: int,
    languages: list[str],
    primary_language: str,
) -> dict:
    """
    批量加载 Content 的所有多语言字段。

    返回 {field_name: {language: value}}，已含回退：
    其他语言无 i18n 记录时，自动复制主语言的值。

    回退规则：
        主语言值从 EntityI18n 获取（若无则后续由 objects.py 从模型字段取）；
        其他语言若 EntityI18n 中无记录，则回退复制主语言的值。
    """
    stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "content",
        EntityI18n.entity_id == content_id,
        EntityI18n.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    raw: dict[str, dict[str, str]] = {}
    for row in rows:
        if row.value and row.field_name and row.language:
            raw.setdefault(row.field_name, {})[row.language] = row.value

    result: dict[str, dict[str, str]] = {}
    for field_name, lang_values in raw.items():
        primary_value = lang_values.get(primary_language)
        result[field_name] = {}
        for lang in languages:
            if lang in lang_values:
                result[field_name][lang] = lang_values[lang]
            elif primary_value:
                result[field_name][lang] = primary_value

    return result


async def _load_movie_i18n_data(
    db: AsyncSession,
    movies: list[Movie],
    languages: list[str],
    primary_language: str,
) -> dict:
    """
    批量加载 Movie 的多语言数据。

    返回 {movie_id: {field_name: {language: value}}}，含回退。
    """
    if not movies:
        return {}

    movie_ids = [m.id for m in movies]
    stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "movie",
        EntityI18n.entity_id.in_(movie_ids),
        EntityI18n.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    raw: dict[int, dict[str, dict[str, str]]] = {}
    for row in rows:
        if row.value and row.field_name and row.language:
            raw.setdefault(row.entity_id, {}).setdefault(row.field_name, {})[row.language] = row.value

    result: dict[int, dict[str, dict[str, str]]] = {}
    for mid in movie_ids:
        if mid not in raw:
            continue
        result[mid] = {}
        for field_name, lang_values in raw[mid].items():
            primary_value = lang_values.get(primary_language)
            result[mid][field_name] = {}
            for lang in languages:
                if lang in lang_values:
                    result[mid][field_name][lang] = lang_values[lang]
                elif primary_value:
                    result[mid][field_name][lang] = primary_value

    return result


# ═══════════════════════════════════════════════════════════
# Cast 多语言加载
# ═══════════════════════════════════════════════════════════
async def _load_cast_i18n_data(
    db: AsyncSession,
    casts: list[Cast],
    languages: list[str],
    primary_language: str,
) -> dict:
    """
    批量加载 Cast 的多语言数据。

    返回 {cast_id: {field_name: {language: value}}}，含回退。
    """
    if not casts:
        return {}

    cast_ids = [c.id for c in casts]
    stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "cast",
        EntityI18n.entity_id.in_(cast_ids),
        EntityI18n.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    raw: dict[int, dict[str, dict[str, str]]] = {}
    for row in rows:
        if row.value and row.field_name and row.language:
            raw.setdefault(row.entity_id, {}).setdefault(row.field_name, {})[row.language] = row.value

    result: dict[int, dict[str, dict[str, str]]] = {}
    for cid in cast_ids:
        if cid not in raw:
            continue
        result[cid] = {}
        for field_name, lang_values in raw[cid].items():
            primary_value = lang_values.get(primary_language)
            result[cid][field_name] = {}
            for lang in languages:
                if lang in lang_values:
                    result[cid][field_name][lang] = lang_values[lang]
                elif primary_value:
                    result[cid][field_name][lang] = primary_value

    return result


async def _load_category_i18n_data(
    db: AsyncSession,
    categories: list[Category],
    languages: list[str],
    primary_language: str,
) -> dict:
    """
    批量加载 Category 的多语言数据。

    返回 {category_id: {field_name: {language: value}}}，含回退。
    """
    if not categories:
        return {}

    cat_ids = [c.id for c in categories]
    stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "category",
        EntityI18n.entity_id.in_(cat_ids),
        EntityI18n.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    raw: dict[int, dict[str, dict[str, str]]] = {}
    for row in rows:
        if row.value and row.field_name and row.language:
            raw.setdefault(row.entity_id, {}).setdefault(row.field_name, {})[row.language] = row.value

    result: dict[int, dict[str, dict[str, str]]] = {}
    for cid in cat_ids:
        if cid not in raw:
            continue
        result[cid] = {}
        for field_name, lang_values in raw[cid].items():
            primary_value = lang_values.get(primary_language)
            result[cid][field_name] = {}
            for lang in languages:
                if lang in lang_values:
                    result[cid][field_name][lang] = lang_values[lang]
                elif primary_value:
                    result[cid][field_name][lang] = primary_value

    return result


async def _load_schedule_actor_director(
    db: AsyncSession,
    casts: list[Cast],
    cast_role_maps: list[CastRoleMap],
    languages: list[str],
    primary_language: str,
) -> tuple[dict, dict]:
    """
    聚合 Schedule 的 Actor/Director 多语言名称。

    从 CastRoleMap 中按 role_name 分组（Actor/Director），
    再从 Cast + EntityI18n 中获取各语言的名称，逗号分隔。

    返回 ({language: "actor1,actor2"}, {language: "dir1,dir2"})。
    """
    actor_cast_ids = [rm.cast_id for rm in cast_role_maps if rm.role_name and rm.role_name.lower() == "actor"]
    director_cast_ids = [rm.cast_id for rm in cast_role_maps if rm.role_name and rm.role_name.lower() == "director"]

    cast_map = {c.id: c for c in casts}
    all_ids = set(actor_cast_ids + director_cast_ids)
    if not all_ids:
        return {}, {}

    stmt = select(EntityI18n).where(
        func.lower(EntityI18n.entity_type) == "cast",
        EntityI18n.entity_id.in_(all_ids),
        EntityI18n.field_name == "name",
        EntityI18n.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    cast_i18n: dict[int, dict[str, str]] = {}
    for row in rows:
        if row.value and row.language:
            cast_i18n.setdefault(row.entity_id, {})[row.language] = row.value

    def aggregate_by_lang(ids: list[int]) -> dict[str, str]:
        lang_names: dict[str, list[str]] = {}
        for cid in ids:
            cast = cast_map.get(cid)
            if not cast:
                continue
            for lang in languages:
                name = cast_i18n.get(cid, {}).get(lang) or (cast.name if lang == primary_language else None)
                if name:
                    lang_names.setdefault(lang, []).append(name)
        return {lang: ",".join(names) for lang, names in lang_names.items()}

    return aggregate_by_lang(actor_cast_ids), aggregate_by_lang(director_cast_ids)


async def _load_schedule_package_ids(
    db: AsyncSession,
    schedule_meta: ScheduleMetadata | None,
) -> str | None:
    """
    查询 Schedule 的 PPV PackageID（逗号分隔）。

    解析顺序：
        1. ScheduleMetadata.package_ids：Schedule 自身配置的服务包
        2. ScheduleMetadata.series_id：归档目标连续剧（格式 "Series_<id>"），
           解析出 Series content id 后查询该 Series 关联的服务包

    返回如 "Package_1,Package_2" 或 None。
    """
    from loguru import logger

    logger.info(f"[_load_schedule_package_ids] schedule_meta: {schedule_meta}")
    if schedule_meta:
        logger.info(
            f"[_load_schedule_package_ids] package_ids: {schedule_meta.package_ids}, "
            f"series_id: {schedule_meta.series_id}"
        )

    if not schedule_meta:
        logger.info("[_load_schedule_package_ids] 返回 None - schedule_meta 为空")
        return None

    package_ids: list[int] = []
    if schedule_meta.package_ids:
        package_ids.extend(int(pid) for pid in schedule_meta.package_ids)

    # 归档场景：仅当 Schedule 自身未配置服务包时，才通过 series_id 解析出
    # Series content id，查询其关联服务包作为兜底。若 Schedule 已显式配置
    # package_ids，应优先使用用户选择的服务包，避免 XML 与界面不一致。
    if not package_ids and schedule_meta.series_id:
        series_content_id = _parse_series_id(schedule_meta.series_id)
        if series_content_id is not None:
            series_packages = await _get_content_packages(db, series_content_id)
            for pkg in series_packages:
                if pkg.id not in package_ids:
                    package_ids.append(pkg.id)
            logger.info(
                f"[_load_schedule_package_ids] series_id={schedule_meta.series_id} "
                f"→ series_content_id={series_content_id}, "
                f"找到 {len(series_packages)} 个服务包"
            )

    if not package_ids:
        logger.info("[_load_schedule_package_ids] 返回 None - 未找到任何服务包")
        return None

    result = await _fmt_packages_by_ids(db, package_ids)
    logger.info(f"[_load_schedule_package_ids] 返回: {result}")
    return result


async def _fmt_packages_by_ids(db: AsyncSession, package_ids: list[int]) -> str | None:
    """将服务包 ID 列表格式化为 "Package_1,Package_2" 逗号分隔串（过滤已删除）。"""
    if not package_ids:
        return None
    stmt = select(Package).where(
        Package.id.in_(package_ids),
        Package.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return None
    return ",".join(f"{ElementType.PACKAGE.value}_{p.id}" for p in rows)


async def _get_archive_content_ids(db: AsyncSession, schedule_id: int) -> list[int]:
    """查询节目单的归档产物内容 ID（EPISODE/MOVIE，未删除/废弃）。"""
    return list(
        (
            await db.execute(
                select(Content.id).where(
                    Content.source_schedule_id == schedule_id,
                    Content.is_archived.is_(True),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).scalars().all()
    )


async def _get_archive_content_packages(db: AsyncSession, archive_ids: list[int]) -> list[Package]:
    """
    按产物顺序去重聚合归档产物关联的服务包。

    归档场景下连续剧单集即归档产物，其服务包对应 C2 规范中
    “订阅后可观看每集节目” 的 PackageID 语义。
    """
    packages: list[Package] = []
    seen: set[int] = set()
    for cid in archive_ids:
        for pkg in await _get_content_packages(db, cid):
            if pkg.id not in seen:
                seen.add(pkg.id)
                packages.append(pkg)
    return packages


async def _load_series_package_ids(
    db: AsyncSession,
    schedule_meta: ScheduleMetadata | None,
) -> str | None:
    """
    查询 Schedule 关联 Series 的服务包，用于输出 Series 段的 PackageID。

    C2 规范：当 series 支持 PPV subscription 时，SeriesType 段里可输出 PackageID，
    表示订阅该服务包后可观看连续剧每集。与 PPVEnable 的 PackageID 属性含义不同。

    取值顺序（归档场景修复：归档流程仅继承许可证/题材，不向归档目标连续剧
    写服务包中间表，且节目单服务包仅存于 ScheduleMetadata.package_ids）：
        1. Series 内容关联的服务包（ContentPackage 中间表）
        2. 归档产物（EPISODE/MOVIE）关联的服务包
        3. 节目单自身 PPV 服务包（ScheduleMetadata.package_ids）
    回退 2/3 仅在该节目单存在归档产物时才启用（归档闸门），
    非归档场景在第 1 级查空后直接返回 None，与修复前行为完全一致。

    返回如 "Package_1,Package_2" 或 None。
    """
    from loguru import logger

    if not schedule_meta or not schedule_meta.series_id:
        return None

    series_content_id = _parse_series_id(schedule_meta.series_id)
    if series_content_id is not None:
        series_packages = await _get_content_packages(db, series_content_id)
        if series_packages:
            result = ",".join(f"{ElementType.PACKAGE.value}_{p.id}" for p in series_packages)
            logger.info(
                f"[_load_series_package_ids] series_id={schedule_meta.series_id} "
                f"→ {len(series_packages)} 个服务包: {result}"
            )
            return result
        logger.info(
            f"[_load_series_package_ids] series_id={schedule_meta.series_id} "
            f"未找到关联服务包，进入归档场景回退"
        )

    # 归档闸门：仅当该节目单存在归档产物时才启用回退；
    # 非归档场景在此直接返回 None，与修复前行为完全一致，零影响
    archive_ids = await _get_archive_content_ids(db, schedule_meta.content_id)
    if not archive_ids:
        logger.info(
            f"[_load_series_package_ids] schedule_id={schedule_meta.content_id} "
            f"无归档产物，按原逻辑返回 None"
        )
        return None

    # 回退 1：归档产物关联的服务包（归档节目即连续剧下的单集）
    archive_packages = await _get_archive_content_packages(db, archive_ids)
    if archive_packages:
        result = ",".join(f"{ElementType.PACKAGE.value}_{p.id}" for p in archive_packages)
        logger.info(
            f"[_load_series_package_ids] schedule_id={schedule_meta.content_id} "
            f"→ 回退归档产物服务包: {result}"
        )
        return result

    # 回退 2：节目单自身 PPV 服务包
    if schedule_meta.package_ids:
        result = await _fmt_packages_by_ids(db, list(schedule_meta.package_ids))
        if result:
            logger.info(
                f"[_load_series_package_ids] schedule_id={schedule_meta.content_id} "
                f"→ 回退节目单自身服务包: {result}"
            )
            return result

    return None


async def _load_series_children_count(
    db: AsyncSession,
    schedule_meta: ScheduleMetadata | None,
) -> int | None:
    """
    统计 Schedule 关联 Series 下的实际集数，用于 Schedule XML 输出 VolumnCount。

    C2 规范：VolumnCount 为 series 必填（Total number of episodes in a series）。
    ScheduleMetadata.volume_count 是创建时写入的静态字段（常为 NULL，且增删
    集数后不同步），故与 Series 对象同口径取实时子内容数：统计 series_id 下
    未删除/未作废的全部直接子节点（不过滤类型，兼容 SERIES 下挂
    SEASON_SERIES/SEASON 的层级，与 _get_children 口径一致）。无关联 Series
    时返回 None（由调用方回退到 meta.volume_count）。
    """
    from loguru import logger

    if not schedule_meta or not schedule_meta.series_id:
        return None

    series_content_id = _parse_series_id(schedule_meta.series_id)
    if series_content_id is None:
        return None

    count = (
        await db.execute(
            select(func.count()).select_from(Content).where(
                Content.parent_id == series_content_id,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalar() or 0
    logger.info(
        f"[_load_series_children_count] series_id={schedule_meta.series_id} "
        f"→ {count} 集"
    )
    return count


def _parse_series_id(series_id: str | None) -> int | None:
    """解析 series_id（格式 "Series_<id>" 或纯数字）为 content id。"""
    if not series_id:
        return None
    raw = series_id.strip()
    # 去掉 "Series_" 前缀
    if raw.startswith(f"{ElementType.SERIES.value}_"):
        raw = raw[len(ElementType.SERIES.value) + 1:]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# ═══════════════════════════════════════════════════════════
# PosterSize 名称映射
# ═══════════════════════════════════════════════════════════
async def _load_poster_size_names(
    db: AsyncSession, pictures: list[Picture]
) -> tuple[dict, dict]:
    """
    加载 Picture 关联的 PosterSize 名称和 mapping_type。

    返回 (names_dict, mapping_types_dict)：
        names_dict:       {picture_id: poster_size_name}
        mapping_types_dict: {picture_id: mapping_type}（仅 mapping_type 非空）
    """
    if not pictures:
        return {}, {}
    ps_ids = list({p.poster_size_id for p in pictures if p.poster_size_id})
    if not ps_ids:
        return {}, {}
    stmt = select(PosterSize).where(
        PosterSize.id.in_(ps_ids),
        PosterSize.is_deleted.is_(False),
    )
    sizes = list((await db.execute(stmt)).scalars().all())
    size_map = {s.id: s.name for s in sizes if s.name}
    mapping_type_map = {s.id: s.mapping_type for s in sizes if s.mapping_type is not None}
    names = {
        p.id: size_map.get(p.poster_size_id, None)
        for p in pictures
        if p.poster_size_id and p.poster_size_id in size_map
    }
    mapping_types = {
        p.id: mapping_type_map.get(p.poster_size_id, None)
        for p in pictures
        if p.poster_size_id and p.poster_size_id in mapping_type_map
    }
    return names, mapping_types
