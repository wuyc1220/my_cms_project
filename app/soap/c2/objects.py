"""
C2 规范 Object 元素构建器。

每个函数对应一种 ElementType，输入 ORM 对象与 Action，
返回 `xml.etree.ElementTree.Element`，形如::

    <Object ElementType="Program" ID="Program_123" Code="Program_123" Action="REGIST">
        <Property Name="Name" Language="en">SuperMan</Property>
        <Property Name="Name" Language="tr">SuperMan</Property>
        <Property Name="ShortTitle" Language="en">SM</Property>
        <Property Name="ShortTitle" Language="tr">SM</Property>
        ...
    </Object>

约定：
1. DELETE 的 Object 仅保留 ElementType + ID + Code，无 Property
2. REGIST / UPDATE 均输出全量属性（UPDATE 增量优化可后续迭代）
3. None / 空字符串的 Property 一律跳过，减少 LSP 端解析压力
4. 所有 ID 统一转为字符串；布尔转为 "true"/"false"
5. 数组字段按逗号分隔字符串输出
6. 规范要求 ID 和 Code 格式为 ElementType_ID（如 Series_3），全局唯一且必填
7. Property 支持 XML 属性（如 Type="RTUK"、Language="en"）

多语言回退规则：
    - 第一种语言（主语言）从模型字段取值
    - 其他语言从 i18n_data 获取
    - 若某语言无 i18n 记录，回退复制主语言的值（loader 已处理）
"""
from __future__ import annotations

import json
import re
from typing import Any
from xml.etree.ElementTree import Element, SubElement

from app.internal.cms_biz_metada.models.basic import Cast, Category, Picture
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
    Package,
    PhysicalChannel,
)

from app.config import app_tz

from .constants import Action, ElementType


# ═══════════════════════════════════════════════════════════
# 通用工具函数
# ═══════════════════════════════════════════════════════════
def _build_file_url(file_path: str | None) -> str | None:
    if not file_path:
        return None
    return file_path


def _make_object(element_type: ElementType, action: Action, obj_id: Any) -> Element:
    """
    创建 <Object> 骨架元素。

    规范要求 ID 和 Code 格式为 ElementType_ID（如 Series_3），全局唯一且必填：
        <Object ElementType="X" ID="X_ID" Code="X_ID" Action="Y"/>
    """
    typed_id = f"{element_type.value}_{obj_id}"
    return Element(
        "Object",
        {
            "ElementType": element_type.value,
            "ID": typed_id,
            "Code": typed_id,
            "Action": action.value,
        },
    )


def _fmt_c2_id(value: Any, element_type: ElementType) -> str | None:
    """
    将裸 ID 格式化为 C2 规范的 ElementType_ID 格式（如 Series_3）。

    若 value 为 None 则返回 None；若已是 "ElementType_xxx" 格式则原样返回；
    否则拼接为 "{ElementType}_{value}"。
    """
    if value is None:
        return None
    prefix = f"{element_type.value}_"
    if isinstance(value, str) and value.startswith(prefix):
        return value
    return f"{prefix}{value}"


def _add_property(
    parent: Element,
    name: str,
    value: Any,
    attrs: dict[str, str] | None = None,
) -> None:
    """
    向 <Object> 追加一个 <Property Name="xxx" ...>value</Property>。

    None / 空字符串 / 空列表一律跳过。
    布尔值转 "true"/"false"，列表转 "a,b,c"，其它 str()。

    :param attrs: Property 的额外 XML 属性，如 {"Type": "RTUK"}、{"Language": "en"}
    """
    if value is None:
        return
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (list, tuple)):
        if not value:
            return
        text = ",".join(str(v) for v in value if v is not None and v != "")
        if not text:
            return
    elif isinstance(value, str):
        if value == "":
            return
        text = value
    else:
        text = str(value)

    attr_dict = {"Name": name}
    if attrs:
        attr_dict.update(attrs)
    prop = SubElement(parent, "Property", attr_dict)
    prop.text = text


def _add_i18n_property(
    parent: Element,
    name: str,
    primary_value: Any,
    i18n_data: dict,
    field_name: str,
    languages: list[str],
    primary_language: str,
    extra_attrs: dict[str, str] | None = None,
) -> None:
    """
    向 <Object> 追加多语言 Property，含回退逻辑。

    规则：
        - 主语言：优先从 i18n_data 取，若无则使用 primary_value（模型字段值）
        - 其他语言：从 i18n_data 取，若无则回退复制主语言值
        - 每种语言输出一个 <Property Name="xxx" Language="yyy">value</Property>

    :param parent: 父 Element
    :param name: Property Name（如 "Name", "ShortTitle"）
    :param primary_value: 模型字段的默认值（主语言回退值）
    :param i18n_data: {field_name: {language: value}} 字典；
           当 field_name="" 时，i18n_data 本身就是 {language: value}
    :param field_name: i18n_data 中的字段键名（如 "name", "short_title"）；
           传空字符串表示 i18n_data 已经是 {language: value} 格式
    :param languages: 所有语言代码列表
    :param primary_language: 主语言代码
    :param extra_attrs: 额外 XML 属性（如 Type="RTUK"）
    """
    if field_name:
        field_i18n = i18n_data.get(field_name, {})
    else:
        field_i18n = i18n_data

    for lang in languages:
        if lang == primary_language:
            value = field_i18n.get(lang, primary_value)
        else:
            value = field_i18n.get(lang)
            if value is None:
                value = field_i18n.get(primary_language, primary_value)

        if value is None or value == "":
            continue

        attrs = {"Language": lang}
        if extra_attrs:
            attrs.update(extra_attrs)
        _add_property(parent, name, value, attrs=attrs)


def _fmt_sections_info(sections_info: Any) -> str | None:
    """
    将 sections_info（JSONB 数组）格式化为 C2 规范要求的 JSON 字符串。

    C2 规范要求所有值为字符串类型，输出格式如：
        [{"type":"1","action":"0","tag":"intro","start":"0","end":"60"},...]

    :param sections_info: 数据库中的 JSONB 值（Python list[dict] 或 None）
    :return: JSON 字符串或 None
    """
    if not sections_info:
        return None
    # C2 规范固定字段顺序：type -> action -> tag -> start -> end
    key_order = ("type", "action", "tag", "start", "end")
    formatted = []
    for item in sections_info:
        ordered = {k: str(item[k]) for k in key_order if k in item}
        ordered.update({k: str(v) for k, v in item.items() if k not in ordered})
        formatted.append(ordered)
    return json.dumps(formatted, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════
# 1. Program — Content(MOVIE/EPISODE) + ContentMetadata
# ═══════════════════════════════════════════════════════════
def build_program_object(
    content: Content,
    meta: ContentMetadata | None,
    action: Action,
    license_data: dict | None = None,
    genre_name: str | None = None,
    genre_i18n: dict | None = None,
    content_type_name: str | None = None,
    custom_fields: dict | None = None,
    tag_names: str | None = None,
    tag_i18n: dict | None = None,
    i18n_data: dict | None = None,
    languages: list[str] | None = None,
    primary_language: str = "en",
) -> Element:
    """
    构建 Program 对象。

    规范必填：VodType, Name, Genre, Type, RatingLevel, ContentTier, SeriesFlag
    许可证聚合字段：LicensingWindowStart/End, ContentTier, Provider,
                   Downloadable, LicenseDuration, Previewable, AdvertorialRights
    多语言字段：Name, ShortTitle, Description, OriginalName, SortName, Genre, Tags
    """
    obj = _make_object(ElementType.PROGRAM, action, content.id)
    if action == Action.DELETE:
        return obj

    langs = languages or [primary_language]
    i18n = i18n_data or {}
    g_i18n = genre_i18n or {}
    t_i18n = tag_i18n or {}

    _add_property(obj, "VodType", meta.vod_type if meta else None)
    # 名称取子表 program_metadata.name（归档内容应从子表获取），meta 缺失时回退到 content.title
    _add_i18n_property(obj, "Name", meta.name if meta else content.title, i18n, "name", langs, primary_language)
    _add_i18n_property(obj, "Genre", genre_name, g_i18n, "", langs, primary_language)
    _add_property(obj, "Type", content_type_name)
    _add_property(obj, "RatingLevel", meta.rating_level if meta else None, attrs={"Type": "RTUK"})
    _add_property(obj, "SeriesFlag", meta.series_flag if meta else None)

    if license_data:
        _add_property(obj, "ContentTier", license_data.get("content_tier"))
        _add_property(obj, "LicensingWindowStart", license_data.get("licensing_window_start"))
        _add_property(obj, "LicensingWindowEnd", license_data.get("licensing_window_end"))
        _add_property(obj, "Provider", license_data.get("provider"))

        downloadable = license_data.get("downloadable")
        dl_attrs = None
        if downloadable:
            dl_attrs = {}
            ld = license_data.get("license_duration")
            if ld is not None:
                dl_attrs["LicenseDuration"] = str(ld)
            if not dl_attrs:
                dl_attrs = None
        _add_property(obj, "Downloadable", downloadable, attrs=dl_attrs)

        previewable = license_data.get("previewable")
        pv_attrs = {}
        pb = license_data.get("preview_begin_time")
        pe = license_data.get("preview_end_time")
        if pb:
            pv_attrs["Begintime"] = pb
        if pe:
            pv_attrs["Endtime"] = pe
        _add_property(obj, "Previewable", previewable, attrs=pv_attrs if pv_attrs else None)

        _add_property(obj, "AdvertorialRights", license_data.get("advertorial_rights"))

    _add_i18n_property(obj, "SortName", meta.sort_name if meta else None, i18n, "sort_name", langs, primary_language)
    _add_i18n_property(obj, "ShortTitle", meta.short_title if meta else None, i18n, "short_title", langs, primary_language)
    _add_property(obj, "OriginalName", meta.original_name if meta else None)
    _add_property(obj, "OriginalCountry", meta.original_country if meta else None)
    _add_property(obj, "Language", meta.language if meta else None)
    _add_property(obj, "ReleaseYear", meta.release_year if meta else None)
    _add_i18n_property(obj, "Description", meta.description if meta else None, i18n, "description", langs, primary_language)
    _add_property(obj, "Status", "1" if (meta.status_flag if meta else False) else "0")
    _add_i18n_property(obj, "Tags", tag_names, t_i18n, "", langs, primary_language)
    _add_property(obj, "Advice", meta.advice if meta else None)
    # Rating 字段支持 type 和 ID 属性（如 IMDB 评分），人工输入时 type 和 ID 为空字符串
    if meta and meta.rating:
        rating_attrs = {"type": meta.rating_type or "", "ID": meta.rating_id or ""}
        _add_property(obj, "Rating", meta.rating, attrs=rating_attrs)
    _add_property(obj, "AudioLang", meta.audio_lang if meta else None)
    _add_property(obj, "SubtitleLang", meta.subtitle_lang if meta else None)
    _add_property(obj, "Studio", meta.studio if meta else None)
    _add_property(obj, "CDR_ID", meta.cdr_id if meta else None)
    _add_property(obj, "BeginDuration", meta.begin_duration if meta else None)
    _add_property(obj, "EndDuration", meta.end_duration if meta else None)
    _add_property(obj, "SectionsInfo", _fmt_sections_info(meta.sections_info) if meta else None)
    _add_property(obj, "Keywords", meta.keywords if meta else None)
    _add_property(obj, "Metalayout", meta.metalayout if meta else None)

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 2. Movie — 媒资文件
# ═══════════════════════════════════════════════════════════
def build_movie_object(
    movie: Movie,
    action: Action,
    i18n_data: dict | None = None,
    languages: list[str] | None = None,
    primary_language: str = "en",
    custom_fields: dict | None = None,
) -> Element:
    """
    构建 Movie 对象（正片/预告片/字幕）。

    规范必填：Type, FileURL, Duration, Definition, Encryption
    规范属性名：Type(1=Movie/2=Trailer/3=Subtitle), FileURL(JSON下载URL)
    C2 规范：字幕（Type=3）REGIST 不要求 Duration/Definition，数据存 NULL 时自动不输出
    多语言字段：Name（仅 Trailer 输出，C2 规范：when type is "Trailer", this param is required）
    布尔字段输出 0/1：Encryption, ClosedCaptioning, PublishFlag
    """
    obj = _make_object(ElementType.MOVIE, action, movie.id)
    if action == Action.DELETE:
        return obj

    langs = languages or [primary_language]
    i18n = i18n_data or {}

    _add_property(obj, "Type", movie.movie_type)
    # PublishFlag=1(YES)时输出 FileURL，PublishFlag=0(NO)时跳转（使用 Deeplink）
    if movie.publish_flag:
        _add_property(obj, "FileURL", _build_file_url(movie.file_path))
    else:
        # PublishFlag=0(NO)时输出 Deeplink
        _add_property(obj, "Deeplink", movie.deeplink)
    # 字幕（Type=3）未填写时数据为 NULL，_add_property 自动跳过不输出
    _add_property(obj, "Duration", movie.duration)
    _add_property(obj, "Definition", movie.definition)
    _add_property(obj, "Encryption", "1" if movie.encryption else "0")

    _add_property(obj, "AudioType", movie.audio_type)
    _add_property(obj, "ScreenFormat", movie.screen_format)
    _add_property(obj, "ClosedCaptioning", "1" if movie.closed_captioning else "0")
    # FileSize: PublishFlag=0(DeepLink场景)时填写0；PublishFlag=1时按实际值或0
    if movie.publish_flag:
        _add_property(obj, "FileSize", movie.file_size if movie.file_size is not None else "0")
    else:
        _add_property(obj, "FileSize", "0")
    _add_property(obj, "PublishFlag", "1" if movie.publish_flag else "0")
    # Deeplink 已在上面根据 PublishFlag 条件添加
    _add_property(obj, "Mediaservice", movie.mediaservice if hasattr(movie, "mediaservice") else None)
    _add_property(obj, "Sequence", movie.sequence if hasattr(movie, "sequence") else None)

    # C2 规范：Name 为 Trailer 名称（多语言），仅 type=2(Trailer) 时输出
    if int(movie.movie_type) == 2:
        _add_i18n_property(obj, "Name", movie.file_name, i18n, "name", langs, primary_language)

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 3. Series — Content(SERIES/SEASON) + SeriesMetadata
# ═══════════════════════════════════════════════════════════
def build_series_object(
    content: Content,
    meta: SeriesMetadata | None,
    action: Action,
    license_data: dict | None = None,
    genre_name: str | None = None,
    genre_i18n: dict | None = None,
    content_type_name: str | None = None,
    custom_fields: dict | None = None,
    tag_names: str | None = None,
    tag_i18n: dict | None = None,
    i18n_data: dict | None = None,
    languages: list[str] | None = None,
    primary_language: str = "en",
    children_count: int = 0,
) -> Element:
    """
    构建 Series 对象。SERIES/SEASON 共用此类型，通过 SeriesType 区分。

    规范必填：VodType, Name, Genre, Type, RatingLevel, VolumnCount, SeriesType, CDR_ID
    多语言字段：Name, ShortTitle, Description, OriginalName, SortName, Genre, Tags
    """
    obj = _make_object(ElementType.SERIES, action, content.id)
    if action == Action.DELETE:
        return obj

    langs = languages or [primary_language]
    i18n = i18n_data or {}
    g_i18n = genre_i18n or {}
    t_i18n = tag_i18n or {}

    _add_property(obj, "VodType", meta.vod_type if meta else None)
    # 名称取子表 series_metadata.name，meta 缺失时回退到 content.title
    _add_i18n_property(obj, "Name", meta.name if meta else content.title, i18n, "name", langs, primary_language)
    _add_i18n_property(obj, "Genre", genre_name, g_i18n, "", langs, primary_language)
    _add_property(obj, "Type", content_type_name)
    _add_property(obj, "RatingLevel", meta.rating_level if meta else None, attrs={"Type": "RTUK"})

    # VolumnCount / SeriesType 由系统维护，meta 中可能为 None
    # 根据 content_type 自动推导 SeriesType：SERIES→1, SEASON_SERIES→2, SEASON→3
    st = meta.series_type if (meta and meta.series_type is not None) else content.series_type
    if st is None:
        st = {"SEASON": 3, "SEASON_SERIES": 2}.get(content.content_type, 1)
    # VolumnCount 始终取实际子内容数量，不使用 meta.volume_count 静态值
    # （meta.volume_count 是创建时写入的，增删子内容后不会同步更新）
    _add_property(obj, "VolumnCount", children_count)
    _add_property(obj, "SeriesType", st)
    _add_property(obj, "CDR_ID", meta.cdr_id if meta else None)

    if license_data:
        _add_property(obj, "ContentTier", license_data.get("content_tier"))
        _add_property(obj, "LicensingWindowStart", license_data.get("licensing_window_start"))
        _add_property(obj, "LicensingWindowEnd", license_data.get("licensing_window_end"))
        _add_property(obj, "Provider", license_data.get("provider"))

        downloadable = license_data.get("downloadable")
        dl_attrs = None
        if downloadable:
            dl_attrs = {}
            ld = license_data.get("license_duration")
            if ld is not None:
                dl_attrs["LicenseDuration"] = str(ld)
            if not dl_attrs:
                dl_attrs = None
        _add_property(obj, "Downloadable", downloadable, attrs=dl_attrs)

        _add_property(obj, "AdvertorialRights", license_data.get("advertorial_rights"))

    _add_i18n_property(obj, "SortName", meta.sort_name if meta else None, i18n, "sort_name", langs, primary_language)
    _add_i18n_property(obj, "ShortTitle", meta.short_title if meta else None, i18n, "short_title", langs, primary_language)
    _add_property(obj, "OriginalName", meta.original_name if meta else None)
    _add_property(obj, "OriginalCountry", meta.original_country if meta else None)
    _add_property(obj, "Language", meta.language if meta else None)
    _add_property(obj, "ReleaseYear", meta.release_year if meta else None)
    _add_i18n_property(obj, "Description", meta.description if meta else None, i18n, "description", langs, primary_language)
    _add_property(obj, "Status", "1" if (meta.status_flag if meta else False) else "0")
    _add_i18n_property(obj, "Tags", tag_names, t_i18n, "", langs, primary_language)
    _add_property(obj, "Advice", meta.advice if meta else None)
    # Rating 字段支持 type 和 ID 属性（如 IMDB 评分），人工输入时 type 和 ID 为空字符串
    if meta and meta.rating:
        rating_attrs = {"type": meta.rating_type or "", "ID": meta.rating_id or ""}
        _add_property(obj, "Rating", meta.rating, attrs=rating_attrs)
    _add_property(obj, "AudioLang", meta.audio_lang if meta else None)
    _add_property(obj, "SubtitleLang", meta.subtitle_lang if meta else None)
    _add_property(obj, "Studio", meta.studio if meta else None)
    # SeriesOrdinal: meta 优先，回退到 content.series_ordinal
    so = meta.series_ordinal if (meta and meta.series_ordinal is not None) else content.series_ordinal
    _add_property(obj, "SeriesOrdinal", so)
    # ShowID: meta 优先；SEASON_SERIES 时从 parent_id 推导；统一 C2 格式
    sid = meta.show_id if (meta and meta.show_id is not None) else None
    if sid is None and content.content_type == "SEASON_SERIES" and content.parent_id:
        sid = content.parent_id
    _add_property(obj, "ShowID", _fmt_c2_id(sid, ElementType.SERIES))
    _add_property(obj, "Keywords", meta.keywords if meta else None)
    _add_property(obj, "Metalayout", meta.metalayout if meta else None)

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 4. Schedule — Content(SCHEDULE) + ScheduleMetadata
# ═══════════════════════════════════════════════════════════
def build_schedule_object(
    content: Content,
    meta: ScheduleMetadata | None,
    action: Action,
    license_data: dict | None = None,
    channel_id: str | None = None,
    channel_code: str | None = None,
    genre_name: str | None = None,
    genre_i18n: dict | None = None,
    custom_fields: dict | None = None,
    i18n_data: dict | None = None,
    languages: list[str] | None = None,
    primary_language: str = "en",
    schedule_actor_i18n: dict | None = None,
    schedule_director_i18n: dict | None = None,
    schedule_package_ids: str | None = None,
    series_package_ids: str | None = None,
    series_children_count: int | None = None,
    pictures: list | None = None,
    poster_size_mapping_types: dict | None = None,
) -> Element:
    """
    构建 Schedule 对象。

    规范必填：ChannelID, ChannelCode, ProgramName, Genre, BeginTime, EndTime, RatingLevel
    多语言字段：ProgramName, Description, Genre, SeriesName, ShowName, Actor, Director
    布尔字段输出 0/1：CUTVEnable, TSTVEnable, TSTVMode, NPVREnable
    PPVEnable 始终输出 0/1，当值为 1 时额外输出 PackageID/PreBuffer/PostBuffer/PurchaseBeginTime/PurchaseEndTime 字段
    """
    obj = _make_object(ElementType.SCHEDULE, action, content.id)
    if action == Action.DELETE:
        return obj

    langs = languages or [primary_language]
    i18n = i18n_data or {}
    g_i18n = genre_i18n or {}
    a_i18n = schedule_actor_i18n or {}
    d_i18n = schedule_director_i18n or {}

    _add_property(obj, "ChannelID", _fmt_c2_id(channel_id or content.parent_id, ElementType.CHANNEL))
    _add_property(obj, "ChannelCode", _fmt_c2_id(channel_code or content.parent_id, ElementType.CHANNEL))
    # 节目单名称取子表 schedule_metadata.name，meta 缺失时回退到 content.title
    _add_i18n_property(obj, "ProgramName", meta.name if meta else content.title, i18n, "name", langs, primary_language)
    _add_i18n_property(obj, "Genre", genre_name, g_i18n, "", langs, primary_language)
    _add_property(obj, "BeginTime", _fmt_dt(content.begin_time))
    _add_property(obj, "EndTime", _fmt_dt(content.end_time))
    _add_property(obj, "Status", "1" if (meta.status_flag if meta else False) else "0")
    _add_i18n_property(obj, "Description", meta.description if meta else None, i18n, "description", langs, primary_language)
    # 根据 C2 规范：CUTVEnable=1 时 ProgramID 必须作为 CUTVEnable 的 XML 属性输出。
    cutv_enable = bool(content.cutv_enable)
    cutv_attrs: dict[str, str] = {}
    if cutv_enable:
        program_id = _fmt_c2_id(meta.program_id if meta else None, ElementType.PROGRAM)
        if program_id:
            cutv_attrs["ProgramID"] = program_id
    _add_property(obj, "CUTVEnable", "1" if cutv_enable else "0", attrs=cutv_attrs or None)
    if license_data:
        _add_property(obj, "ContentTier", license_data.get("content_tier"))
    _add_property(obj, "RatingLevel", meta.rating_level if meta else None, attrs={"Type": "RTUK"})
    _add_property(obj, "BroadcastType", meta.broadcast_type if meta else None)
    # Actor/Director: 每种语言输出，无值时回退到主语言
    for lang in langs:
        actor_val = a_i18n.get(lang) or a_i18n.get(primary_language)
        if actor_val:
            _add_property(obj, "Actor", actor_val, attrs={"Language": lang})
        director_val = d_i18n.get(lang) or d_i18n.get(primary_language)
        if director_val:
            _add_property(obj, "Director", director_val, attrs={"Language": lang})
    _add_property(obj, "AudioLang", meta.audio_lang if meta else None)
    _add_property(obj, "SubtitleLang", meta.subtitle_lang if meta else None)
    _add_property(obj, "TSTVEnable", "1" if (meta.tstv_enable if meta else True) else "0")
    _add_property(obj, "SectionsInfo", _fmt_sections_info(meta.sections_info) if meta else None)
    _add_property(obj, "TSTVMode", "1" if (meta.tstv_mode if meta else False) else "0")
    _add_property(obj, "NPVREnable", "1" if (meta.npvr_enable if meta else True) else "0")

    if pictures:
        ps_mt = poster_size_mapping_types or {}
        for pic in pictures:
            if pic.entity_type.lower() != "schedule":
                continue
            mapping_type = ps_mt.get(pic.id, -1)
            _add_property(obj, "PictureID", _fmt_c2_id(pic.id, ElementType.PICTURE), attrs={"Type": str(mapping_type)})

    ppv_enable = meta.ppv_enable if meta else False
    # 根据 C2 规范：PPVEnable 始终输出，当值为 1 时 PackageID/PreBuffer/PostBuffer/
    # PurchaseBeginTime/PurchaseEndTime 必须作为 PPVEnable 的 XML 属性输出。
    ppv_attrs: dict[str, str] = {}
    if ppv_enable:
        if schedule_package_ids:
            ppv_attrs["PackageID"] = schedule_package_ids
        if meta:
            if meta.pre_buffer is not None:
                ppv_attrs["PreBuffer"] = str(meta.pre_buffer)
            if meta.post_buffer is not None:
                ppv_attrs["PostBuffer"] = str(meta.post_buffer)
            if meta.purchase_begin_time is not None:
                ppv_attrs["PurchaseBeginTime"] = str(meta.purchase_begin_time)
            if meta.purchase_end_time is not None:
                ppv_attrs["PurchaseEndTime"] = str(meta.purchase_end_time)
    _add_property(obj, "PPVEnable", "1" if ppv_enable else "0", attrs=ppv_attrs or None)

    if meta:
        _add_property(obj, "SeriesType", meta.series_type)
        _add_i18n_property(obj, "SeriesName", meta.series_name, i18n, "series_name", langs, primary_language)
        _add_property(obj, "SeriesID", _fmt_c2_id(meta.series_id, ElementType.SERIES))
        # VolumnCount：优先取关联 Series 的实时集数（与 Series 对象同口径）；
        # meta.volume_count 是创建时写入的静态字段，常为 NULL 导致属性缺失
        vc = series_children_count if series_children_count is not None else meta.volume_count
        _add_property(obj, "VolumnCount", vc)
        _add_property(obj, "Sequence", meta.sequence)
        # C2 规范：series 支持 PPV subscription 时，在 SeriesType 段输出 PackageID，
        # 表示订阅该服务包后可观看连续剧每集。与 PPVEnable 的 PackageID 属性含义不同。
        if meta.series_type and meta.series_type != 0 and series_package_ids:
            _add_property(obj, "PackageID", series_package_ids)
        _add_property(obj, "SeriesOrdinal", meta.series_ordinal)
        _add_property(obj, "ShowID", _fmt_c2_id(meta.show_id, ElementType.SERIES))
        _add_i18n_property(obj, "ShowName", meta.show_name, i18n, "show_name", langs, primary_language)
        _add_property(obj, "CDR_ID", meta.cdr_id)
        _add_property(obj, "Advice", meta.advice)
        # 节目单 XML 不输出 Studio 属性（需求 32052）

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 5. Channel — Content(CHANNEL) + ChannelMetadata
# ═══════════════════════════════════════════════════════════
def build_channel_object(
    content: Content,
    meta: ChannelMetadata | None,
    action: Action,
    license_data: dict | None = None,
    provider_code: str | None = None,
    custom_fields: dict | None = None,
    i18n_data: dict | None = None,
    languages: list[str] | None = None,
    primary_language: str = "en",
    pictures: list | None = None,
    poster_size_mapping_types: dict | None = None,
) -> Element:
    """
    构建 Channel（逻辑频道）对象。

    规范必填：Name, Type, RatingLevel, Status
    多语言字段：Name, Description
    """
    obj = _make_object(ElementType.CHANNEL, action, content.id)
    if action == Action.DELETE:
        return obj

    langs = languages or [primary_language]
    i18n = i18n_data or {}

    # 频道名称取子表 channel_metadata.name，meta 缺失时回退到 content.title
    _add_i18n_property(obj, "Name", meta.name if meta else content.title, i18n, "name", langs, primary_language)
    _add_property(obj, "Type", meta.channel_type if meta else None)
    _add_property(obj, "RatingLevel", meta.rating_level if meta else None, attrs={"Type": "RTUK"})
    _add_property(obj, "Status", "1" if (meta.status_flag if meta else False) else "0")

    if license_data:
        _add_property(obj, "ContentTier", license_data.get("content_tier"))
        _add_property(obj, "AdvertorialRights", license_data.get("advertorial_rights"))

    _add_property(obj, "ChannelNumber", meta.channel_number if meta else None)
    _add_i18n_property(obj, "Description", meta.description if meta else None, i18n, "description", langs, primary_language)
    _add_property(obj, "AudioType", meta.audio_type if meta else None)
    _add_property(obj, "AudioLang", meta.audio_lang if meta else None)
    _add_property(obj, "SubtitleLang", meta.subtitle_lang if meta else None)
    # 根据 C2 规范：PPVEnable 始终输出，但 Channel 对象没有 PPV 相关属性
    _add_property(obj, "PPVEnable", "1" if (meta.ppv_enable if meta else False) else "0")
    _add_property(obj, "NPVREnable", "1" if (meta.npvr_enable if meta else 0) else "0")
    _add_property(obj, "FingerprintEnable", meta.fingerprint_enable if meta else None)
    _add_property(obj, "WatermarkEnable", meta.watermark_enable if meta else None)
    _add_property(obj, "Cpcode", provider_code)

    if pictures:
        ps_mt = poster_size_mapping_types or {}
        for pic in pictures:
            if pic.entity_type.lower() != "channel":
                continue
            mapping_type = ps_mt.get(pic.id, -1)
            _add_property(obj, "PictureID", _fmt_c2_id(pic.id, ElementType.PICTURE), attrs={"Type": str(mapping_type)})

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 6. PhysicalChannel — 物理频道
# ═══════════════════════════════════════════════════════════
def build_physical_channel_object(
    pc: PhysicalChannel,
    action: Action,
    channel_type: str | None = None,
    channel_name: str | None = None,
    channel_description: str | None = None,
    custom_fields: dict | None = None,
) -> Element:
    """
    构建 PhysicalChannel 对象。

    规范必填：ChannelID, Name, Type, Definition, TVODEnable, TSTVEnable, CUTVEnable, Encryption
    ChannelID = 关联的 Channel 的 ID
    Name = 所属 Channel 的名称
    Description = 所属 Channel 的描述
    Type = 从父 Channel 的 channel_type 获取
    布尔字段输出 0/1：TVODEnable, TSTVEnable, CUTVEnable, Encryption
    """
    obj = _make_object(ElementType.PHYSICAL_CHANNEL, action, pc.id)
    if action == Action.DELETE:
        return obj

    _add_property(obj, "ChannelID", _fmt_c2_id(pc.channel_id, ElementType.CHANNEL))
    _add_property(obj, "Name", channel_name)
    _add_property(obj, "Description", channel_description)
    _add_property(obj, "Type", channel_type)
    _add_property(obj, "Mediaservice", pc.mediaservice)
    _add_property(obj, "Definition", pc.definition)
    _add_property(obj, "Videoencode", pc.videoencode)
    _add_property(obj, "Bitrate", pc.bitrate)
    _add_property(obj, "DeeplinkChURL", pc.deeplink_ch_url)
    _add_property(obj, "Shifttime", pc.shifttime)
    _add_property(obj, "Tvodsavetime", pc.tvod_save_time)
    _add_property(obj, "TVODEnable", "1" if pc.tvod_enable else "0")
    _add_property(obj, "TSTVEnable", "1" if pc.tstv_enable else "0")
    _add_property(obj, "CUTVEnable", "1" if pc.cutv_enable else "0")
    _add_property(obj, "Encryption", "1" if pc.encryption else "0")

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 7. Category — 栏目分类
# ═══════════════════════════════════════════════════════════
def build_category_object(
    cat: Category,
    action: Action,
    i18n_data: dict | None = None,
    languages: list[str] | None = None,
    primary_language: str = "en",
    custom_fields: dict | None = None,
    poster_type: str | int | None = None,
) -> Element:
    """
    构建 Category 对象。

    规范必填：ParentCode(att), ParentID, Name, Sequence, Status
    规范要求 ParentCode 作为 Object 属性（与 ID/Code 同级）
    多语言字段：Name, Description

    :param poster_type: 保留入参以兼容既有调用点；bug 32017 处理策略为
                       "PosterType / JumpCategoryCode 字段先保留、内容传空"，
                       因此当前该值不参与 XML 输出，待 C2 规范明确后再恢复。
    """
    obj = _make_object(ElementType.CATEGORY, action, cat.id)
    if action == Action.DELETE:
        return obj

    langs = languages or [primary_language]
    i18n = i18n_data or {}

    parent_id_val = _fmt_c2_id(cat.parent_id, ElementType.CATEGORY) or "0"
    obj.set("ParentCode", parent_id_val)

    _add_property(obj, "ParentID", parent_id_val)
    _add_i18n_property(obj, "Name", cat.name, i18n, "name", langs, primary_language)
    _add_property(obj, "Sequence", cat.sequence)
    _add_property(obj, "Status", cat.status)
    _add_i18n_property(obj, "Description", cat.description, i18n, "description", langs, primary_language)
    _add_property(obj, "CategoryType", cat.category_type)
    _add_property(obj, "VodCount", cat.vod_count)

    # bug 32017：JumpCategoryCode / PosterType 字段先保留、内容传空
    # 原逻辑：JumpCategoryCode 取 cat.jump_category_code；
    #        PosterType 取首张关联海报的 PosterSize.mapping_type（多海报时会丢类型）
    # 待 C2 规范明确后再恢复取值，此处直接输出空 Property 占位
    SubElement(obj, "Property", {"Name": "JumpCategoryCode"})
    SubElement(obj, "Property", {"Name": "PosterType"})

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 8. Cast — 演职员
# ═══════════════════════════════════════════════════════════
def build_cast_object(
    cast: Cast,
    action: Action,
    i18n_data: dict | None = None,
    languages: list[str] | None = None,
    primary_language: str = "en",
    custom_fields: dict | None = None,
) -> Element:
    """
    构建 Cast 对象。

    规范必填：Name
    多语言字段：Name, Description
    """
    obj = _make_object(ElementType.CAST, action, cast.id)
    if action == Action.DELETE:
        return obj

    langs = languages or [primary_language]
    i18n = i18n_data or {}

    _add_i18n_property(obj, "Name", cast.name, i18n, "name", langs, primary_language)
    _add_i18n_property(obj, "Description", cast.description, i18n, "description", langs, primary_language)

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 9. CastRoleMap — 角色映射
# ═══════════════════════════════════════════════════════════
def build_cast_role_map_object(rm: CastRoleMap, action: Action, custom_fields: dict | None = None) -> Element:
    """
    构建 CastRoleMap 对象。

    规范必填：CastRole, CastID, CastCode
    CastCode = CastID（规范要求同值）
    CastRole（规范属性名，非 RoleName）
    """
    obj = _make_object(ElementType.CAST_ROLE_MAP, action, rm.map_id)
    if action == Action.DELETE:
        return obj

    _add_property(obj, "CastRole", rm.role_name)
    _add_property(obj, "CastID", _fmt_c2_id(rm.cast_id, ElementType.CAST))
    _add_property(obj, "CastCode", _fmt_c2_id(rm.cast_id, ElementType.CAST))

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 10. Picture — 图片
# ═══════════════════════════════════════════════════════════
def build_picture_object(
    pic: Picture,
    action: Action,
    poster_size_name: str | None = None,
    custom_fields: dict | None = None,
) -> Element:
    """
    构建 Picture 对象。

    规范必填：FileURL
    PictureType: 1=common picture, 2=schedule picture
    Description: 从 poster_size.name 获取
    """
    obj = _make_object(ElementType.PICTURE, action, pic.id)
    if action == Action.DELETE:
        return obj

    _add_property(obj, "FileURL", _build_file_url(pic.file_path))
    picture_type = 2 if pic.entity_type.lower() == "schedule" else 1
    _add_property(obj, "PictureType", picture_type)
    _add_property(obj, "Description", poster_size_name)

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 11. Package — 服务包
# ═══════════════════════════════════════════════════════════
def build_package_object(pkg: Package, action: Action, custom_fields: dict | None = None) -> Element:
    """
    构建 Package 对象。

    规范必填：Name
    """
    obj = _make_object(ElementType.PACKAGE, action, pkg.id)
    if action == Action.DELETE:
        return obj

    _add_property(obj, "Name", pkg.name)
    _add_property(obj, "Description", pkg.description)

    if custom_fields and custom_fields.get("extendinfo"):
        _add_property(obj, "Extendinfo", custom_fields.get("extendinfo"))

    return obj


# ═══════════════════════════════════════════════════════════
# 辅助：日期格式化
# ═══════════════════════════════════════════════════════════
def _fmt_dt(dt) -> str | None:
    """日期格式化，将ORM中的UTC datetime转回本地时区后输出 (2026-07-17T00:00:00.000Z)"""
    if dt is None:
        return None
    # ORM 中的 datetime 已被 asyncpg 自动转为 UTC，需转回本地时区再格式化
    localized = dt.astimezone(app_tz)
    return localized.strftime("%Y-%m-%dT%H:%M:%S") + f".{localized.microsecond // 1000:03d}Z"


# ═════════════════════════════════════════════════════════
# 辅助：空 Property 显式开闭标签后处理
# ═════════════════════════════════════════════════════════
# bug 32017：Category 的 JumpCategoryCode / PosterType 字段先保留传空，
# 需要输出 <Property Name="X"></Property> 而非自闭合 <Property Name="X"/>。
# ElementTree + minidom 对无 text 的元素都会输出自闭合形式，
# 因此在序列化后统一做一次字符串替换。
EMPTY_PROPERTY_NAMES: tuple[str, ...] = ("JumpCategoryCode", "PosterType")
_EMPTY_PROPERTY_RE = re.compile(
    r'<Property\s+Name="(' + "|".join(EMPTY_PROPERTY_NAMES) + r')"\s*/>'
)


def expand_empty_properties(xml_str: str) -> str:
    """
    将指定 Name 的自闭合 <Property .../> 改写为显式开闭标签
    <Property Name="X"></Property>，用于 bug 32017 中“字段保留传空”的
    输出要求。其他 Property 不受影响。
    """
    if not xml_str:
        return xml_str
    return _EMPTY_PROPERTY_RE.sub(
        lambda m: f'<Property Name="{m.group(1)}"></Property>',
        xml_str,
    )
