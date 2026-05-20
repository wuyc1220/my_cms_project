"""
元数据扩展 Pydantic 数据模型。

覆盖 Program / Series / Channel / Schedule 四种类型的元数据 CRUD。
按原型规格 3.6.2.1 ~ 3.6.2.4 对齐，与 ORM 模型一致。
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


# ═══════════════════════════════════════════════════════════
# 公共子结构
# ═══════════════════════════════════════════════════════════

class SectionInfoItem(BaseModel):
    """章节信息单条记录。"""
    type: int
    action: int
    tag: str
    start: int
    end: int


# ═══════════════════════════════════════════════════════════
# 1. ContentMetadata — Program (MOVIE / EPISODE)
# ═══════════════════════════════════════════════════════════

class ContentMetadataBase(BaseModel):
    """Program 元数据基础字段（创建/更新共用）。"""
    name: str = Field(..., max_length=100)
    # genre_id 用于校验，但不保存到元数据表（存储在 content 主表）
    genre_id: int | None = None
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
    cdr_id: str = Field(..., max_length=100)
    series_flag: int = 0
    begin_duration: Optional[int] = None
    end_duration: Optional[int] = None
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
    # genre_id 用于校验，但不保存到元数据表（存储在 content 主表）
    genre_id: Optional[int] = None
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
    begin_duration: Optional[int] = None
    end_duration: Optional[int] = None
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
    # genre_id 用于校验，但不保存到元数据表（存储在 content 主表）
    genre_id: int | None = None
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
    cdr_id: str = Field(..., max_length=100)
    begin_duration: Optional[int] = None
    end_duration: Optional[int] = None
    status_flag: bool = True
    keywords: Optional[list[str]] = None
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
    # genre_id 用于校验，但不保存到元数据表（存储在 content 主表）
    genre_id: Optional[int] = None
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
    begin_duration: Optional[int] = None
    end_duration: Optional[int] = None
    status_flag: Optional[bool] = None
    keywords: Optional[list[str]] = None
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
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    channel_number: Optional[int] = None
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
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表
    channel_number: Optional[int] = None
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
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
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
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
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
