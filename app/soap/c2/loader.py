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
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
    categories: list[Category] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    movies: list[Movie] = field(default_factory=list)
    cast_role_maps: list[CastRoleMap] = field(default_factory=list)
    casts: list[Cast] = field(default_factory=list)
    pictures: list[Picture] = field(default_factory=list)

    # Channel 专用：关联的 PhysicalChannel
    physical_channels: list[PhysicalChannel] = field(default_factory=list)

    # SERIES/SEASON 专用：子 Content 列表（EPISODE 或子 SERIES）
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

    # 自定义字段
    custom_fields: dict = field(default_factory=dict)

    # Tags 名称（tag_ids → Tag.name，按主语言查询，逗号分隔）
    tag_names: str | None = None

    # Tags 多语言 {language: "tag1,tag2"}
    tag_i18n: dict = field(default_factory=dict)

    # CustomField 多语言 {language: json_str}
    custom_field_i18n: dict = field(default_factory=dict)

    # Cast 多语言数据 {cast_id: {field_name: {language: value}}}
    cast_i18n_data: dict = field(default_factory=dict)

    # Category 多语言数据 {category_id: {field_name: {language: value}}}
    category_i18n_data: dict = field(default_factory=dict)

    # Schedule Actor/Director 聚合 {language: "actor1,actor2"} / {language: "dir1,dir2"}
    schedule_actor_i18n: dict = field(default_factory=dict)
    schedule_director_i18n: dict = field(default_factory=dict)

    # Schedule PPV PackageID（逗号分隔）
    schedule_package_ids: str | None = None

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
    elif ct in ("SERIES", "SEASON"):
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

    elif ct in ("SERIES", "SEASON"):
        ctx.children = await _get_children(db, content_id)
        ctx.cast_role_maps = await _get_cast_role_maps(db, content_id)
        ctx.casts = await _get_casts_of_role_maps(db, ctx.cast_role_maps)

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
    ctx.custom_fields = await _load_custom_fields(db, content_id)

    # ── 10.1 自定义字段多语言 ─────────────────────────────
    ctx.custom_field_i18n = await _load_custom_field_i18n(db, content_id, ctx.languages, ctx.primary_language)

    # ── 10.2 Tags 名称（tag_ids → Tag.name）─────────────
    ctx.tag_names, ctx.tag_i18n = await _load_tag_names(db, content, ctx.languages)

    # ── 11. PosterSize 名称映射 ───────────────────────────
    ctx.poster_size_names = await _load_poster_size_names(db, ctx.pictures)

    # ── 12. Movie 多语言数据 ─────────────────────────────
    if ctx.movies:
        ctx.movie_i18n_data = await _load_movie_i18n_data(db, ctx.movies, ctx.languages, ctx.primary_language)

    # ── 13. Cast 多语言数据 ──────────────────────────────
    if ctx.casts:
        ctx.cast_i18n_data = await _load_cast_i18n_data(db, ctx.casts, ctx.languages, ctx.primary_language)

    # ── 14. Category 多语言数据 ─────────────────────────
    if ctx.categories:
        ctx.category_i18n_data = await _load_category_i18n_data(db, ctx.categories, ctx.languages, ctx.primary_language)

    # ── 15. Schedule Actor/Director 聚合 + PackageID ───
    if ct == "SCHEDULE" and ctx.casts:
        ctx.schedule_actor_i18n, ctx.schedule_director_i18n = await _load_schedule_actor_director(
            db, ctx.casts, ctx.cast_role_maps, ctx.languages, ctx.primary_language
        )
        ctx.schedule_package_ids = await _load_schedule_package_ids(db, ctx.schedule_meta)

    return ctx


# ═══════════════════════════════════════════════════════════
# 私有加载函数
# ═══════════════════════════════════════════════════════════
async def _get_content(db: AsyncSession, content_id: int) -> Content | None:
    """查询单条 Content（过滤软删除）"""
    stmt = select(Content).where(
        and_(Content.id == content_id, Content.is_deleted.is_(False))
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _get_one(db: AsyncSession, model, where_expr):
    """通用单条查询（过滤软删除）"""
    stmt = select(model).where(and_(where_expr, model.is_deleted.is_(False)))
    return (await db.execute(stmt)).scalar_one_or_none()


async def _get_content_categories(db: AsyncSession, content_id: int) -> list[Category]:
    """查询 Content 关联的所有 Category"""
    stmt = (
        select(Category)
        .join(ContentCategory, ContentCategory.category_id == Category.id)
        .where(
            ContentCategory.content_id == content_id,
            ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False),
            Category.is_deleted.is_(False),
        )
    )
    return list((await db.execute(stmt)).scalars().all())


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
    )
    return list((await db.execute(stmt)).scalars().all())


def _picture_entity_type(content_type: str) -> str:
    """Content.content_type → Picture.entity_type 的反向映射（小写，与数据库一致）"""
    if content_type in ("MOVIE", "EPISODE"):
        return "program"
    if content_type in ("SERIES", "SEASON"):
        return "series"
    if content_type == "CHANNEL":
        return "channel"
    if content_type == "SCHEDULE":
        return "schedule"
    return ""


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
        result["licensing_window_start"] = min(start_dates).isoformat()
    if end_dates:
        result["licensing_window_end"] = max(end_dates).isoformat()
    if all_platforms:
        result["content_tier"] = ",".join(sorted(all_platforms))
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
        result["advertorial_rights"] = ",".join(sorted(ad_platforms))

    return result


# ═══════════════════════════════════════════════════════════
# Genre / ContentType / Provider 加载
# ═══════════════════════════════════════════════════════════
async def _load_genre_name(db: AsyncSession, content: Content) -> str | None:
    """从 genre_id 查询 Genre 名称（主语言）"""
    genre_id = content.genre_id
    if genre_id is None:
        return None
    genre = await _get_one(db, Genre, Genre.id == genre_id)
    return genre.name if genre else None


async def _load_genre_i18n(db: AsyncSession, content: Content, languages: list[str]) -> dict:
    """
    从 genre_id 查询 Genre 多语言名称。

    Genre 表每行一个语言变体，通过 language 字段区分。
    返回 {language: name}，如 {"en": "Action", "tr": "Aksiyon"}。
    """
    genre_id = content.genre_id
    if genre_id is None:
        return {}

    stmt = select(Genre).where(
        Genre.id == genre_id,
        Genre.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return {g.language: g.name for g in rows if g.language and g.name}


async def _load_tag_names(
    db: AsyncSession, content: Content, languages: list[str]
) -> tuple[str | None, dict]:
    """
    从 ContentMetadata.tag_ids 查询 Tag 名称。

    Tag 表每行一个语言变体，通过 language 字段区分。
    返回 (主语言逗号分隔名称, {language: 逗号分隔名称})。
    """
    meta = None
    if content.content_type in ("MOVIE", "EPISODE"):
        meta = await _get_one(db, ContentMetadata, ContentMetadata.content_id == content.id)
    elif content.content_type in ("SERIES", "SEASON"):
        meta = await _get_one(db, SeriesMetadata, SeriesMetadata.content_id == content.id)
    elif content.content_type == "SCHEDULE":
        meta = await _get_one(db, ScheduleMetadata, ScheduleMetadata.content_id == content.id)

    tag_ids = None
    if meta and hasattr(meta, "tag_ids"):
        tag_ids = meta.tag_ids
    if not tag_ids:
        return None, {}

    stmt = select(Tag).where(
        Tag.id.in_(tag_ids),
        Tag.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    lang_tags: dict[str, list[str]] = {}
    for tag in rows:
        if tag.language and tag.name:
            lang_tags.setdefault(tag.language, []).append(tag.name)

    tag_i18n = {lang: ",".join(names) for lang, names in lang_tags.items()}
    primary = tag_i18n.get(languages[0]) if languages else None
    return primary, tag_i18n


async def _load_custom_field_i18n(
    db: AsyncSession, content_id: int, languages: list[str], primary_language: str
) -> dict:
    """
    从 EntityFieldValue 加载自定义字段的多语言值。

    CustomField 有 multi_language 标记，多语言字段按语言分别存储。
    非多语言字段只输出到主语言，避免所有语言重复。
    返回 {language: json_str}，如 {"en": '{"key":"val"}', "tr": '{"key":"val2"}'}。
    """
    stmt = select(EntityFieldValue).where(
        EntityFieldValue.entity_type == "Content",
        EntityFieldValue.entity_id == content_id,
        EntityFieldValue.is_deleted.is_(False),
    )
    values = list((await db.execute(stmt)).scalars().all())
    if not values:
        return {}

    import json

    lang_fields: dict[str, dict[str, str]] = {}
    for ev in values:
        if not ev.custom_field_id:
            continue
        cf = await _get_one(db, CustomField, CustomField.id == ev.custom_field_id)
        if not cf:
            continue
        if cf.field_code in ("extendinfo", "Extendinfo"):
            continue
        if cf.multi_language:
            for lang in languages:
                i18n_stmt = select(EntityI18n).where(
                    EntityI18n.entity_type == "CustomField",
                    EntityI18n.entity_id == content_id,
                    EntityI18n.field_name == cf.field_code,
                    EntityI18n.language == lang,
                    EntityI18n.is_deleted.is_(False),
                )
                i18n_row = (await db.execute(i18n_stmt)).scalar_one_or_none()
                if i18n_row and i18n_row.value:
                    lang_fields.setdefault(lang, {})[cf.field_code] = i18n_row.value
        else:
            lang_fields.setdefault(primary_language, {})[cf.field_code] = ev.value or ""

    result = {}
    for lang, fields in lang_fields.items():
        if fields:
            result[lang] = json.dumps(fields, ensure_ascii=False)
    return result


async def _load_content_type_name(db: AsyncSession, content: Content) -> str | None:
    """从 content_type 映射到 C2 规范的 Type 值"""
    type_id = None
    meta = None
    if content.content_type in ("MOVIE", "EPISODE"):
        meta = await _get_one(db, ContentMetadata, ContentMetadata.content_id == content.id)
    elif content.content_type in ("SERIES", "SEASON"):
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


# ═══════════════════════════════════════════════════════════
# 自定义字段加载
# ═══════════════════════════════════════════════════════════
async def _load_custom_fields(db: AsyncSession, content_id: int) -> dict:
    """
    从 EntityFieldValue 加载自定义字段。

    返回 {"custom_field": "...", "extendinfo": "..."} 格式。
    """
    stmt = select(EntityFieldValue).where(
        EntityFieldValue.entity_type == "Content",
        EntityFieldValue.entity_id == content_id,
        EntityFieldValue.is_deleted.is_(False),
    )
    values = list((await db.execute(stmt)).scalars().all())
    if not values:
        return {}

    custom_field_parts = {}
    extendinfo_parts = {}
    for ev in values:
        if ev.custom_field_id:
            cf = await _get_one(db, CustomField, CustomField.id == ev.custom_field_id)
            if cf:
                if cf.field_code in ("extendinfo", "Extendinfo"):
                    extendinfo_parts[cf.field_code] = ev.value
                else:
                    custom_field_parts[cf.field_code] = ev.value

    result = {}
    if custom_field_parts:
        import json
        result["custom_field"] = json.dumps(custom_field_parts, ensure_ascii=False)
    if extendinfo_parts:
        import json
        result["extendinfo"] = json.dumps(extendinfo_parts, ensure_ascii=False)

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
        EntityI18n.entity_type == "Content",
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
        EntityI18n.entity_type == "Movie",
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
        EntityI18n.entity_type == "cast",
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
        EntityI18n.entity_type == "category",
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
        EntityI18n.entity_type == "cast",
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
    从 ScheduleMetadata.package_ids 查询 Package 编码，逗号分隔。

    返回如 "PKG001,PKG002" 或 None。
    """
    if not schedule_meta or not schedule_meta.package_ids:
        return None

    stmt = select(Package).where(
        Package.id.in_(schedule_meta.package_ids),
        Package.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    if not rows:
        return None

    return ",".join(str(p.id) for p in rows)


# ═══════════════════════════════════════════════════════════
# PosterSize 名称映射
# ═══════════════════════════════════════════════════════════
async def _load_poster_size_names(db: AsyncSession, pictures: list[Picture]) -> dict:
    """加载 Picture 关联的 PosterSize 名称，返回 {picture_id: poster_size_name}"""
    if not pictures:
        return {}
    ps_ids = list({p.poster_size_id for p in pictures if p.poster_size_id})
    if not ps_ids:
        return {}
    stmt = select(PosterSize).where(
        PosterSize.id.in_(ps_ids),
        PosterSize.is_deleted.is_(False),
    )
    sizes = list((await db.execute(stmt)).scalars().all())
    size_map = {s.id: s.name for s in sizes if s.name}
    return {
        p.id: size_map.get(p.poster_size_id, None)
        for p in pictures
        if p.poster_size_id and p.poster_size_id in size_map
    }
