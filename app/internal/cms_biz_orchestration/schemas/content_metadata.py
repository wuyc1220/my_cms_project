"""
元数据扩展 Pydantic 数据模型。

覆盖 Program / Series / Channel / Schedule 四种类型的元数据 CRUD。
按原型规格 3.6.2.1 ~ 3.6.2.4 对齐，与 ORM 模型一致。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# PostgreSQL Integer（4 字节有符号）取值范围，超出会导致数据库写入溢出报 500
INT32_MAX = 2147483647


# ═══════════════════════════════════════════════════════════
# 公共子结构
# ═══════════════════════════════════════════════════════════

class SectionInfoItem(BaseModel):
    """章节信息单条记录。

    标准格式为 int 编码（type: 1=intro/2=ad/3=chapter, action: 0=no skip/1=skip,
    start/end: 秒数）；但存量数据中存在旧字符串格式（如 type='segment',
    action='start', start='00:00:00'，来源为 Excel 导入 json.loads 原样入库），
    读取时放宽兼容，避免元数据详情接口 model_validate 校验失败报 500；
    前端展示有 String 兜底，重新编辑保存后会写回标准格式。
    """
    type: int | str
    action: int | str
    tag: str = ""
    start: int | str
    end: int | str


# C2 规范取值：type 1=intro/2=ad/3=chapter，action 0=no skip/1=skip
_SECTION_TYPE_VALUES = {1, 2, 3}
_SECTION_ACTION_VALUES = {0, 1}


def normalize_sections_info(raw) -> tuple[list[dict] | None, list[str]]:
    """校验并归一化 Excel 导入解析出的 SectionsInfo JSON。

    按 C2 规范校验：type ∈ {1,2,3}、action ∈ {0,1}、start/end 为非负整数秒、
    tag 可选字符串；字符串形式的数字（C2 导出格式如 "1"）自动转为 int。
    导入链路此前只做 json.loads 原样入库，字符串格式脏数据（如
    type='segment'、start='00:00:00'）入库后会导致元数据详情接口校验失败报 500。

    :param raw: json.loads 后的值（list 或其他）
    :return: (归一化结果, 错误列表)；有错误时归一化结果为 None
    """
    errors: list[str] = []
    if not isinstance(raw, list):
        return None, ["SectionsInfo 格式错误，应为 JSON 数组"]
    normalized: list[dict] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"SectionsInfo[{idx}] 应为 JSON 对象")
            continue
        entry: dict = {"tag": str(item.get("tag") or "")}
        for field in ("type", "action", "start", "end"):
            value = item.get(field)
            try:
                int_value = int(str(value).strip())
            except (TypeError, ValueError):
                errors.append(f"SectionsInfo[{idx}].{field} 应为整数，实际为 '{value}'")
                continue
            if field == "type" and int_value not in _SECTION_TYPE_VALUES:
                errors.append(f"SectionsInfo[{idx}].type 应为 1(intro)/2(ad)/3(chapter)，实际为 {int_value}")
            elif field == "action" and int_value not in _SECTION_ACTION_VALUES:
                errors.append(f"SectionsInfo[{idx}].action 应为 0(no skip)/1(skip)，实际为 {int_value}")
            elif field in ("start", "end") and int_value < 0:
                errors.append(f"SectionsInfo[{idx}].{field} 不能为负数，实际为 {int_value}")
            entry[field] = int_value
        normalized.append(entry)
    if errors:
        return None, errors
    return normalized, errors


# ═══════════════════════════════════════════════════════════
# 1. ContentMetadata — Program (MOVIE / EPISODE)
# ═══════════════════════════════════════════════════════════

class ContentMetadataBase(BaseModel):
    """Program 元数据基础字段（创建/更新共用）。"""
    name: str = Field(..., max_length=100)
    # genre_ids 用于校验，但不保存到元数据表（存储在 content_genre 中间表）
    genre_ids: Optional[list[int]] = None
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    vod_type: Optional[list[str]] = None
    sort_name: Optional[str] = Field(None, max_length=100)
    original_name: Optional[str] = Field(None, max_length=100)
    original_country: Optional[str] = Field(None, max_length=100)
    short_title: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = None
    release_year: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    type_id: Optional[int] = None
    tag_ids: Optional[list[int]] = None
    rating_level: Optional[str] = None
    advice: Optional[list[str]] = None
    rating: Optional[str] = None
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    studio: Optional[str] = Field(None, max_length=100)
    cdr_id: Optional[str] = Field(None, max_length=100)
    series_flag: int = 0
    begin_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    end_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    status_flag: bool = True
    keywords: Optional[list[str]] = None
    sections_info: Optional[list[SectionInfoItem]] = None
    metalayout: str = "0"


class ContentMetadataCreate(ContentMetadataBase):
    """创建 Program 元数据请求体。"""
    content_id: int


class ContentMetadataUpdate(BaseModel):
    """更新 Program 元数据请求体（所有字段可选）。"""
    name: Optional[str] = Field(None, max_length=100)
    # genre_ids 用于校验，但不保存到元数据表（存储在 content_genre 中间表）
    genre_ids: Optional[list[int]] = None
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    vod_type: Optional[list[str]] = None
    sort_name: Optional[str] = Field(None, max_length=100)
    original_name: Optional[str] = Field(None, max_length=100)
    original_country: Optional[str] = Field(None, max_length=100)
    short_title: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = None
    release_year: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    type_id: Optional[int] = None
    tag_ids: Optional[list[int]] = None
    rating_level: Optional[str] = None
    advice: Optional[list[str]] = None
    rating: Optional[str] = None
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    studio: Optional[str] = Field(None, max_length=100)
    cdr_id: Optional[str] = Field(None, max_length=100)
    series_flag: Optional[int] = None
    begin_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    end_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    status_flag: Optional[bool] = None
    keywords: Optional[list[str]] = None
    sections_info: Optional[list[SectionInfoItem]] = None
    metalayout: Optional[str] = None


class ContentMetadataItem(ContentMetadataBase):
    """Program 元数据响应项。"""
    id: int
    content_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════
# 2. SeriesMetadata — Series (SERIES / SEASON)
# ═══════════════════════════════════════════════════════════

class SeriesMetadataBase(BaseModel):
    """Series 元数据基础字段。"""
    name: str = Field(..., max_length=100)
    # genre_ids 用于校验，但不保存到元数据表（存储在 content_genre 中间表）
    genre_ids: Optional[list[int]] = None
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    vod_type: Optional[list[str]] = None
    sort_name: Optional[str] = Field(None, max_length=100)
    original_name: Optional[str] = Field(None, max_length=100)
    original_country: Optional[str] = Field(None, max_length=100)
    short_title: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = None
    release_year: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    type_id: Optional[int] = None
    tag_ids: Optional[list[int]] = None
    rating_level: Optional[str] = None
    advice: Optional[list[str]] = None
    rating: Optional[str] = None
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    studio: Optional[str] = Field(None, max_length=100)
    cdr_id: Optional[str] = Field(None, max_length=100)
    begin_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    end_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    status_flag: bool = True
    keywords: Optional[list[str]] = None
    metalayout: str = "0"
    sections_info: Optional[list[SectionInfoItem]] = None
    volume_count: Optional[int] = None
    series_type: Optional[int] = None
    series_ordinal: Optional[int] = None
    show_id: Optional[int] = None


class SeriesMetadataCreate(SeriesMetadataBase):
    """创建 Series 元数据请求体。"""
    content_id: int


class SeriesMetadataUpdate(BaseModel):
    """更新 Series 元数据请求体（所有字段可选）。"""
    name: Optional[str] = Field(None, max_length=100)
    # genre_ids 用于校验，但不保存到元数据表（存储在 content_genre 中间表）
    genre_ids: Optional[list[int]] = None
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    vod_type: Optional[list[str]] = None
    sort_name: Optional[str] = Field(None, max_length=100)
    original_name: Optional[str] = Field(None, max_length=100)
    original_country: Optional[str] = Field(None, max_length=100)
    short_title: Optional[str] = Field(None, max_length=100)
    language: Optional[str] = None
    release_year: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    type_id: Optional[int] = None
    tag_ids: Optional[list[int]] = None
    rating_level: Optional[str] = None
    advice: Optional[list[str]] = None
    rating: Optional[str] = None
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    studio: Optional[str] = Field(None, max_length=100)
    cdr_id: Optional[str] = Field(None, max_length=100)
    begin_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    end_duration: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    status_flag: Optional[bool] = None
    keywords: Optional[list[str]] = None
    metalayout: Optional[str] = None
    sections_info: Optional[list[SectionInfoItem]] = None
    volume_count: Optional[int] = None
    series_type: Optional[int] = None
    series_ordinal: Optional[int] = None
    show_id: Optional[int] = None
    # Update Childs 控制开关（各标签页独立控制）
    update_childs_main: Optional[bool] = False  # Main 标签：元数据字段
    update_childs_custom_fields: Optional[bool] = False  # Custom Fields 标签：自定义字段
    update_childs_i18n: Optional[bool] = False  # Multi Languages 标签：多语言字段


class SeriesMetadataItem(SeriesMetadataBase):
    """Series 元数据响应项。"""
    id: int
    content_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════
# 3. ChannelMetadata — Channel (CHANNEL)
# ═══════════════════════════════════════════════════════════

class ChannelMetadataBase(BaseModel):
    """Channel 元数据基础字段。"""
    name: str = Field(..., max_length=100)
    # 注意：genre_id 已移除，统一使用 content_genre 中间表作为数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    # channel_number 受 int32 范围约束，超限会导致数据库写入溢出报 500
    channel_number: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    description: Optional[str] = Field(None, max_length=500)
    channel_type: Optional[str] = Field(None, max_length=100)
    audio_type: Optional[str] = Field(None, max_length=100)
    rating_level: Optional[str] = None
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    status_flag: bool = True
    ppv_enable: int = 0
    npvr_enable: int = 0
    fingerprint_enable: int = 0
    watermark_enable: int = 0


class ChannelMetadataCreate(ChannelMetadataBase):
    """创建 Channel 元数据请求体。"""
    content_id: int


class ChannelMetadataUpdate(BaseModel):
    """更新 Channel 元数据请求体（所有字段可选）。"""
    name: Optional[str] = Field(None, max_length=100)
    # 注意：genre_id 已移除，统一使用 content_genre 中间表作为数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    # channel_number 受 int32 范围约束，超限会导致数据库写入溢出报 500
    channel_number: Optional[int] = Field(None, ge=0, le=INT32_MAX)
    description: Optional[str] = Field(None, max_length=500)
    channel_type: Optional[str] = Field(None, max_length=100)
    audio_type: Optional[str] = Field(None, max_length=100)
    rating_level: Optional[str] = None
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    status_flag: Optional[bool] = None
    ppv_enable: Optional[int] = None
    npvr_enable: Optional[int] = None
    fingerprint_enable: Optional[int] = None
    watermark_enable: Optional[int] = None


class ChannelMetadataItem(ChannelMetadataBase):
    """Channel 元数据响应项。"""
    id: int
    content_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════
# 4. ScheduleMetadata — Schedule (SCHEDULE)
# ═══════════════════════════════════════════════════════════

class ScheduleMetadataBase(BaseModel):
    """Schedule 元数据基础字段。"""
    name: str = Field(..., max_length=100)
    # 注意：genre_id 已移除，统一使用 content_genre 中间表作为数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    # 注意：begin_time / end_time 已移除，统一使用 content 主表
    vod_type: Optional[list[str]] = None
    type_id: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    rating_level: Optional[str] = None
    advice: Optional[list[str]] = None
    studio: Optional[str] = Field(None, max_length=100)
    cdr_id: Optional[str] = Field(None, max_length=100)
    tag_ids: Optional[list[int]] = None
    status_flag: bool = True
    sections_info: Optional[list[SectionInfoItem]] = None
    # Schedule 专属字段
    cutv_enable: bool = False
    program_id: Optional[str] = Field(None, max_length=100)
    broadcast_type: Optional[str] = Field(None, max_length=100)
    tstv_enable: bool = True
    tstv_mode: bool = False
    npvr_enable: bool = True
    ppv_enable: bool = False
    package_ids: Optional[list[int]] = None
    pre_buffer: Optional[int] = 0
    post_buffer: Optional[int] = 0
    purchase_begin_time: Optional[int] = 180
    purchase_end_time: Optional[int] = -1
    series_type: int = 0
    series_name: Optional[str] = Field(None, max_length=100)
    series_id: Optional[str] = Field(None, max_length=100)
    volume_count: Optional[int] = None
    sequence: Optional[int] = None
    series_ordinal: Optional[int] = None
    show_id: Optional[str] = Field(None, max_length=100)
    show_name: Optional[str] = Field(None, max_length=100)


class ScheduleMetadataCreate(ScheduleMetadataBase):
    """创建 Schedule 元数据请求体。"""
    content_id: int


class ScheduleMetadataUpdate(BaseModel):
    """更新 Schedule 元数据请求体（所有字段可选）。"""
    name: Optional[str] = Field(None, max_length=100)
    # 注意：genre_id 已移除，统一使用 content_genre 中间表作为数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    # 注意：begin_time / end_time 已移除，统一使用 content 主表
    vod_type: Optional[list[str]] = None
    type_id: Optional[int] = None
    description: Optional[str] = Field(None, max_length=500)
    audio_lang: Optional[list[str]] = None
    subtitle_lang: Optional[list[str]] = None
    rating_level: Optional[str] = None
    advice: Optional[list[str]] = None
    studio: Optional[str] = Field(None, max_length=100)
    cdr_id: Optional[str] = Field(None, max_length=100)
    tag_ids: Optional[list[int]] = None
    status_flag: Optional[bool] = None
    sections_info: Optional[list[SectionInfoItem]] = None
    # Schedule 专属字段
    cutv_enable: Optional[bool] = None
    program_id: Optional[str] = Field(None, max_length=100)
    broadcast_type: Optional[str] = Field(None, max_length=100)
    tstv_enable: Optional[bool] = None
    tstv_mode: Optional[bool] = None
    npvr_enable: Optional[bool] = None
    ppv_enable: Optional[bool] = None
    package_ids: Optional[list[int]] = None
    pre_buffer: Optional[int] = None
    post_buffer: Optional[int] = None
    purchase_begin_time: Optional[int] = None
    purchase_end_time: Optional[int] = None
    series_type: Optional[int] = None
    series_name: Optional[str] = Field(None, max_length=100)
    series_id: Optional[str] = Field(None, max_length=100)
    volume_count: Optional[int] = None
    sequence: Optional[int] = None
    series_ordinal: Optional[int] = None
    show_id: Optional[str] = Field(None, max_length=100)
    show_name: Optional[str] = Field(None, max_length=100)


class ScheduleMetadataItem(ScheduleMetadataBase):
    """Schedule 元数据响应项。"""
    id: int
    content_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


# ═══════════════════════════════════════════════════════════
# 5. 统一查询响应
# ═══════════════════════════════════════════════════════════

class MetadataDetailItem(BaseModel):
    """
    内容详情页元数据统一响应。

    根据 content_type 返回对应类型的元数据。
    """
    content_type: str
    content_id: int
    program: Optional[ContentMetadataItem] = None
    series: Optional[SeriesMetadataItem] = None
    channel: Optional[ChannelMetadataItem] = None
    schedule: Optional[ScheduleMetadataItem] = None

    model_config = ConfigDict(from_attributes=True)
