"""
元数据扩展模型。

包含 Program / Series / Channel / Schedule 四种类型的元数据扩展表，
与 content 表一对一关联，通过 content_type 区分适用类型。
按原型规格 3.6.2.1 ~ 3.6.2.4 对齐字段。
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


# ═══════════════════════════════════════════════════════════
# 1. ContentMetadata — Program 元数据 (MOVIE / EPISODE)
# ═══════════════════════════════════════════════════════════

class ContentMetadata(Base):
    """
    Program 元数据扩展表。

    适用内容类型：MOVIE / EPISODE
    与 content 表一对一关联，content_id 唯一。
    对应原型 3.6.2.1 Program 元数据编辑弹框。
    """

    __tablename__ = "program_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="关联内容 ID，FK → content.id",
    )

    # ── 原型 Main 标签页字段 ─────────────────────────────────
    name: Mapped[str] = mapped_column(String(500), nullable=False, comment="主语言名称，初始值默认同 Content Name")
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表作为单一数据源
    vod_type: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="Vod类型数组，来源数据字典 VodType")
    sort_name: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="排序名")
    original_name: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="原名")
    original_country: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="原产地")
    short_title: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="短标题")
    language: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="语言，来源数据字典 Language")
    release_year: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="发行年份")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="简介")
    type_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_type.id", ondelete="SET NULL"),
        nullable=True,
        comment="类型 ID，FK → content_type.id",
    )
    tag_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True, comment="标签 ID 数组")
    rating_level: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="分级，来源数据字典 RatingLevel")
    advice: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True, comment="分级建议数组，来源数据字典 Advice")
    rating: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="评分")
    audio_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="音频语言数组，来源数据字典 Language")
    subtitle_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="字幕语言数组，来源数据字典 Language")
    studio: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="制片公司")
    cdr_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="CDR ID")
    series_flag: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0",
        comment="连续剧标识: 0=VOD(MOVIE), 1=Series(EPISODE)；系统自动判定，界面不显示",
    )
    begin_duration: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="片头时长(秒)")
    end_duration: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="片尾时长(秒)")
    status_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", comment="状态开关: true=YES, false=NO",
    )
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True, comment="关键词数组 [exclusive, hdr]")
    metalayout: Mapped[str] = mapped_column(
        String(50), nullable=False, default="0", server_default="0",
        comment="Metalayout，来源数据字典 Metalayout，默认 0",
    )

    # ── 章节信息 (JSONB) ───────────────────────────────────
    sections_info: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="章节信息 JSONB 数组 [{type, action, tag, start, end}]"
    )

    # ── 审计字段 ───────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    is_discarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="废弃标识"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )


# ═══════════════════════════════════════════════════════════
# 2. SeriesMetadata — Series 元数据 (SERIES / SEASON)
# ═══════════════════════════════════════════════════════════

class SeriesMetadata(Base):
    """
    Series 元数据扩展表。

    适用内容类型：SERIES / SEASON
    对应原型 3.6.2.2 Series 元数据编辑弹框。
    """

    __tablename__ = "series_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="关联内容 ID，FK → content.id",
    )

    # ── 原型 Main 标签页字段 ─────────────────────────────────
    name: Mapped[str] = mapped_column(String(500), nullable=False, comment="主语言名称，初始值默认同 Content Name")
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表作为单一数据源
    vod_type: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="Vod类型数组，来源数据字典 VodType")
    sort_name: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="排序名")
    original_name: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="原名")
    original_country: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="原产地")
    short_title: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="短标题")
    language: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="语言，来源数据字典 Language")
    release_year: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="发行年份")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="简介")
    type_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_type.id", ondelete="SET NULL"),
        nullable=True,
        comment="类型 ID，FK → content_type.id",
    )
    tag_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True, comment="标签 ID 数组")
    rating_level: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="分级，来源数据字典 RatingLevel")
    advice: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True, comment="分级建议数组，来源数据字典 Advice")
    rating: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="评分")
    audio_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="音频语言数组，来源数据字典 Language")
    subtitle_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="字幕语言数组，来源数据字典 Language")
    studio: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="制片公司")
    cdr_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="CDR ID")
    begin_duration: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="片头时长(秒)")
    end_duration: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="片尾时长(秒)")
    status_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", comment="状态开关: true=YES, false=NO",
    )
    keywords: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True, comment="关键词数组 [exclusive, hdr]")

    # ── 章节信息 (JSONB) ───────────────────────────────────
    sections_info: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="章节信息 JSONB 数组 [{type, action, tag, start, end}]"
    )

    # ── Series 专属字段（系统维护，界面不展示）──────────────
    volume_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="集/季数量")
    series_type: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True,
        comment="连续剧类型: 1=普通连续剧, 2=单季连续剧(SERIES时), 3=跨多季连续剧(SEASON时)",
    )
    series_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="季序号，SeriesType=2时有效")
    show_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="总剧ID，SeriesType=2时所属跨多季连续剧的ID")

    # ── 审计字段 ───────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    is_discarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="废弃标识"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )


# ═══════════════════════════════════════════════════════════
# 3. ChannelMetadata — Channel 元数据 (CHANNEL)
# ═══════════════════════════════════════════════════════════

class ChannelMetadata(Base):
    """
    Channel 元数据扩展表。

    适用内容类型：CHANNEL
    对应原型 3.6.2.3 Channel 元数据编辑弹框。
    """

    __tablename__ = "channel_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="关联内容 ID，FK → content.id",
    )

    # ── 原型 Main 标签页字段 ─────────────────────────────────
    name: Mapped[str] = mapped_column(String(500), nullable=False, comment="频道名称，主语言名称")
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表作为单一数据源
    channel_number: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="频道号码")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="简介")
    channel_type: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="频道类型，来源数据字典 Channel_type")
    audio_type: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="音频类型，来源数据字典 AudioType")
    rating_level: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="分级，来源数据字典 RatingLevel")
    audio_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="音频语言数组，来源数据字典 Language")
    subtitle_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="字幕语言数组，来源数据字典 Language")
    language: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="频道语言数组，来源数据字典 Language（需求 3.5.1.2 搜索用）")
    status_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", comment="状态开关: true=YES, false=NO",
    )
    ppv_enable: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", comment="PPV 启用: 0=No, 1=Yes",
    )
    npvr_enable: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", comment="NPVR 启用: 0=No, 1=Yes",
    )
    fingerprint_enable: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", comment="指纹启用: 0=No, 1=Yes",
    )
    watermark_enable: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", comment="水印启用: 0=No, 1=Yes",
    )

    # ── 审计字段 ───────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    is_discarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="废弃标识"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )


# ═══════════════════════════════════════════════════════════
# 4. ScheduleMetadata — Schedule 元数据 (SCHEDULE)
# ═══════════════════════════════════════════════════════════

class ScheduleMetadata(Base):
    """
    Schedule 元数据扩展表。

    适用内容类型：SCHEDULE
    对应原型 3.6.2.4 Schedule 元数据编辑弹框。
    """

    __tablename__ = "schedule_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
        comment="关联内容 ID，FK → content.id",
    )

    # ── 原型 Main 标签页字段 ─────────────────────────────────
    name: Mapped[str] = mapped_column(String(500), nullable=False, comment="节目名称，主语言名称")
    # 注意：genre_id 已移除，统一使用 content.genre_id 作为单一数据源
    # 注意：custom_tag_ids 已移除，统一使用 content_custom_tag 中间表作为单一数据源
    # 注意：begin_time / end_time 已移除，统一使用 content 主表作为单一数据源
    vod_type: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="Vod类型数组")
    type_id: Mapped[int | None] = mapped_column(
        ForeignKey("content_type.id", ondelete="SET NULL"),
        nullable=True,
        comment="类型 ID，FK → content_type.id",
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="简介")
    audio_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="音频语言数组")
    subtitle_lang: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True, comment="字幕语言数组")
    rating_level: Mapped[str | None] = mapped_column(String(20), nullable=True, comment="分级")
    advice: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True, comment="分级建议数组")
    studio: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="制片公司")
    cdr_id: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="CDR ID")
    tag_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True, comment="标签 ID 数组")
    status_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", comment="状态开关",
    )

    # ── 章节信息 (JSONB) ───────────────────────────────────
    sections_info: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, comment="章节信息 JSONB 数组 [{type, action, tag, start, end}]"
    )

    # ── Schedule 专属字段 ──────────────────────────────────
    cutv_enable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="CUTV 启用"
    )
    program_id: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="节目 ID，归档时使用")
    broadcast_type: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="播出类型，来源数据字典 BroadcastType")
    tstv_enable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", comment="TSTV 启用"
    )
    tstv_mode: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="TSTV 模式"
    )
    npvr_enable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", comment="NPVR 启用"
    )
    ppv_enable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="PPV 启用"
    )
    package_ids: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True, comment="服务包 ID 数组（多选）")
    pre_buffer: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0, comment="前缓冲（秒）")
    post_buffer: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0, comment="后缓冲（秒）")
    purchase_begin_time: Mapped[int | None] = mapped_column(Integer, nullable=True, default=180, comment="购买开始时间（分钟），-1 表示无时间控制")
    purchase_end_time: Mapped[int | None] = mapped_column(Integer, nullable=True, default=-1, comment="购买结束时间（分钟），-1 表示无时间控制")
    series_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0", comment="连续剧类型: 0=No, 1=Series, 2=Season Series"
    )
    series_name: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="连续剧名称")
    series_id: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="连续剧 ID")
    volume_count: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="集/季数量，系统自动维护")
    sequence: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="集序号")
    series_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="季序号")
    show_id: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="剧集 ID（总季连续剧）")
    show_name: Mapped[str | None] = mapped_column(String(500), nullable=True, comment="剧集名称（总季连续剧）")

    # ── 审计字段 ───────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    is_discarded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="废弃标识"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )
