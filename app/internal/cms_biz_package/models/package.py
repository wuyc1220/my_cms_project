"""
服务包（Package）领域模型。

包含：
- Package：服务包主表
- PackagePlatform：服务包与平台的多对多关联（一包可部署到多个平台）
- Content：内容最小化模型（用于 Package→Content 关联；完整内容详情由点播/直播模块扩展）
- ContentPackage：内容与服务包的多对多中间表
"""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.internal.cms_biz_metada.models.basic import Category  # noqa: F401  # 正向引用，延迟加载
from app.internal.cms_biz_metada.models.basic import CustomTag  # noqa: F401  # 正向引用，延迟加载
from app.internal.cms_biz_metada.models.basic import Genre  # noqa: F401  # 正向引用，延迟加载


class Package(Base):
    """
    服务包主表。

    字段：
        id          主键
        name        服务包名称，全局唯一
        package_type 服务包类型（来源于数据字典 Package_Type）
        description 描述
        ingest_status Ingest 状态（success/failure），由系统根据关联内容自动维护
        is_deleted  软删除标记
        created_at  创建时间
        updated_at  更新时间

    关系：
        platforms   → PackagePlatform，一包多平台
        contents    → ContentPackage，关联内容中间表
    """

    __tablename__ = "package"
    __table_args__ = (
        Index("uix_package_name_active", "name", unique=True, postgresql_where=sa.text("is_deleted = false")),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, comment="服务包名称，全局唯一")
    package_type: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="服务包类型，来源数据字典 Package_Type")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, comment="描述")
    ingest_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="none", server_default="none", comment="Ingest 状态：none/success/failure/processing"
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    platforms: Mapped[list["PackagePlatform"]] = relationship(
        "PackagePlatform",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    contents: Mapped[list["ContentPackage"]] = relationship(
        "ContentPackage",
        cascade="all, delete-orphan",
        lazy="selectin",
        back_populates="package",
    )


class PackagePlatform(Base):
    """
    服务包平台关联表（一包多平台）。

    字段：
        package_id  外键 → package.id
        platform    平台值（来源数据字典 Platform）
    """

    __tablename__ = "package_platform"

    package_id: Mapped[int] = mapped_column(ForeignKey("package.id", ondelete="CASCADE"), primary_key=True)
    platform: Mapped[str] = mapped_column(String(50), primary_key=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class Content(Base):
    """
    内容主表，覆盖 MOVIE / EPISODE / SERIES / SEASON / CHANNEL / SCHEDULE。

    字段：
        id              主键
        content_type    内容类型枚举：MOVIE/EPISODE/SERIES/SEASON/CHANNEL/SCHEDULE
        title           内容标题
        status          Ingest 状态（None/WaitingForMaterials/Published 等）
        parent_id       父级内容 id（EPISODE→SERIES；单季SERIES→SEASON；SCHEDULE→CHANNEL）
        series_type    仅 SERIES/SEASON 有效：1=普通连续剧，2=单季，3=总季
        sequence        集序号（仅 EPISODE 有效，在父 SERIES 中的顺序）
        series_ordinal  季号（仅 SERIES 有效，在父 SEASON 中的顺序）
        begin_time      节目单开始时间（仅 SCHEDULE 有效）
        end_time        节目单结束时间（仅 SCHEDULE 有效）
        is_deleted      软删除
        created_at      创建时间
        updated_at      更新时间
    """

    __tablename__ = "content"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_type: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
        comment="枚举：MOVIE|EPISODE|SERIES|SEASON|CHANNEL|SCHEDULE"
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False, comment="内容标题")
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="None", server_default="None",
        comment="Ingest 状态，默认 None"
    )
    parent_id: Mapped[int | None] = mapped_column(
        ForeignKey("content.id", ondelete="SET NULL"), nullable=True, index=True,
        comment="父级内容 id（EPISODE→SERIES；单季SERIES→SEASON；SCHEDULE→CHANNEL）"
    )
    series_type: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True,
        comment="1=普通连续剧，2=单季连续剧，3=总季连续剧；仅 SERIES/SEASON 有效"
    )
    sequence: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True,
        comment="集序号，仅 EPISODE 有效"
    )
    series_ordinal: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True,
        comment="季号，仅 SERIES 在 SEASON 下时有效"
    )
    begin_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="节目单开始时间，仅 SCHEDULE 有效"
    )
    end_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="节目单结束时间，仅 SCHEDULE 有效"
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True,
        comment="是否为归档内容：false=普通内容（VOD 可见），true=从直播归档生成（归档管理可见）",
    )
    source_schedule_id: Mapped[int | None] = mapped_column(
        ForeignKey("content.id", ondelete="SET NULL"), nullable=True, index=True,
        comment="归档来源节目单 ID，FK → content.id；仅 is_archived=true 的内容使用",
    )
    archive_scheduled_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="计划归档执行时间，仅 SCHEDULE 有效；mode=plan 时设置，定时任务到期后执行归档",
    )
    cutv_enable: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, server_default="false",
        comment="CUTV 启用开关，仅 SCHEDULE 有效",
    )
    external_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True,
        comment="外部 ID（归档产物：对应 ScheduleMetadata.series_id/show_id，用于反向查找）",
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_discarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", comment="废弃标识，与 is_deleted 逻辑分离")
    previous_status: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="废弃前 Ingest 状态，用于恢复操作",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    packages: Mapped[list["ContentPackage"]] = relationship(
        "ContentPackage",
        cascade="all, delete-orphan",
        lazy="select",
        back_populates="content",
    )
    categories: Mapped[list["ContentCategory"]] = relationship(
        "ContentCategory",
        cascade="all, delete-orphan",
        lazy="select",
        back_populates="content",
    )
    custom_tags: Mapped[list["ContentCustomTag"]] = relationship(
        "ContentCustomTag",
        cascade="all, delete-orphan",
        lazy="select",
        back_populates="content",
    )
    genres: Mapped[list["ContentGenre"]] = relationship(
        "ContentGenre",
        cascade="all, delete-orphan",
        lazy="select",
        back_populates="content",
    )


class ContentPackage(Base):
    """
    内容与服务包的多对多中间表。

    字段：
        id          主键（自增，便于前端 rowKey）
        content_id  外键 → content.id
        package_id  外键 → package.id
        allocated_at 关联创建时间
    """

    __tablename__ = "content_package"
    __table_args__ = (
        UniqueConstraint("content_id", "package_id", name="uq_content_package"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_id: Mapped[int] = mapped_column(
        ForeignKey("package.id", ondelete="CASCADE"), nullable=False, index=True
    )
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    is_discarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", comment="废弃标识")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="packages", lazy="selectin")
    package: Mapped["Package"] = relationship("Package", back_populates="contents", lazy="select")


class ContentCategory(Base):
    """
    内容与栏目的多对多中间表。

    字段：
        id          主键（自增，便于前端 rowKey）
        content_id  外键 → content.id
        category_id 外键 → category.id
        sequence    排序序号（同一栏目下内容的显示顺序）
        allocated_at 关联创建时间
    """

    __tablename__ = "content_category"
    __table_args__ = (
        UniqueConstraint("content_id", "category_id", name="uq_content_category"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True
    )
    category_id: Mapped[int] = mapped_column(
        ForeignKey("category.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sequence: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="排序序号"
    )
    allocated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    is_discarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", comment="废弃标识")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="categories", lazy="selectin")
    category: Mapped["Category"] = relationship("Category", lazy="selectin")


class ContentCustomTag(Base):
    """
    内容与自定义标签的多对多中间表。

    字段：
        id              主键（自增，便于前端 rowKey）
        content_id      外键 → content.id
        custom_tag_id   外键 → custom_tag.id
        created_at      关联创建时间
    """

    __tablename__ = "content_custom_tag"
    __table_args__ = (
        UniqueConstraint("content_id", "custom_tag_id", name="uq_content_custom_tag"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True
    )
    custom_tag_id: Mapped[int] = mapped_column(
        ForeignKey("custom_tag.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="custom_tags", lazy="selectin")
    custom_tag: Mapped["CustomTag"] = relationship("CustomTag", lazy="selectin")


class ContentGenre(Base):
    """
    内容与题材的多对多中间表。

    字段：
        id              主键（自增）
        content_id      外键 → content.id
        genre_id        外键 → genre.id
        created_at      关联创建时间
    """

    __tablename__ = "content_genre"
    __table_args__ = (
        UniqueConstraint("content_id", "genre_id", name="uq_content_genre"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True
    )
    genre_id: Mapped[int] = mapped_column(
        ForeignKey("genre.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    content: Mapped["Content"] = relationship("Content", back_populates="genres", lazy="selectin")
    genre: Mapped["Genre"] = relationship("Genre", lazy="selectin")


class PhysicalChannel(Base):
    """
    物理频道表（归属于业务频道 Content content_type='CHANNEL'）。

    字段：
        id              主键
        channel_id      外键 → content.id（业务频道）
        name            物理频道名称
        channel_number  频道号
        status          状态 YES/NO，默认 YES
        mediaservice    媒体服务
        definition      清晰度
        videoencode     频道编码
        bitrate         频道码率
        deeplink_ch_url 深度链接（JSON）
        shifttime       时移时间
        tvod_save_time  TVOD保存时间
        tvod_enable     TVOD启用
        tstv_enable     TSTV启用
        cutv_enable     CUTVE启用
        encryption      加密
        is_deleted      软删除
        created_at      创建时间
        updated_at      更新时间
    """

    __tablename__ = "physical_channel"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True,
        comment="所属业务频道 id（Content content_type='CHANNEL'）"
    )
    name: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="物理频道名称")
    channel_number: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="频道号")
    status: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true", comment="状态 YES/NO")
    mediaservice: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="媒体服务")
    definition: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="清晰度")
    videoencode: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="频道编码")
    bitrate: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="频道码率")
    deeplink_ch_url: Mapped[str | None] = mapped_column(Text, nullable=True, comment="深度链接（JSON）")
    shifttime: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0", comment="时移时间")
    tvod_save_time: Mapped[int | None] = mapped_column(Integer, nullable=True, server_default="0", comment="TVOD保存时间")
    tvod_enable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, server_default="false", comment="TVOD启用")
    tstv_enable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, server_default="false", comment="TSTV启用")
    cutv_enable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, server_default="false", comment="CUTVE启用")
    encryption: Mapped[bool | None] = mapped_column(Boolean, nullable=True, server_default="true", comment="加密")
    ingest_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="None", server_default="None", comment="Ingest 状态：None/success/failure/processing"
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    is_discarded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false", comment="废弃标识")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class PhysicalChannelHistory(Base):
    """
    物理频道操作历史记录表。

    字段：
        id                  主键
        physical_channel_id 物理频道ID（删除时可能为空）
        channel_id          所属业务频道 id
        name                物理频道名称
        channel_number      频道号
        status              状态 YES/NO
        mediaservice        媒体服务
        definition          清晰度
        videoencode         频道编码
        bitrate             频道码率
        deeplink_ch_url     深度链接
        shifttime           时移时间
        tvod_save_time      TVOD保存时间
        tvod_enable         TVOD启用
        tstv_enable         TSTV启用
        cutv_enable         CUTVE启用
        encryption          加密
        processed_type      操作类型: Add/Update/Delete
        processed_by        操作人
        processed_at        操作时间
    """

    __tablename__ = "physical_channel_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    physical_channel_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True, comment="物理频道ID")
    channel_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True, comment="所属业务频道 id")
    name: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="物理频道名称")
    channel_number: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="频道号")
    status: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="状态 YES/NO")
    mediaservice: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="媒体服务")
    definition: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="清晰度")
    videoencode: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="频道编码")
    bitrate: Mapped[str | None] = mapped_column(String(50), nullable=True, comment="频道码率")
    deeplink_ch_url: Mapped[str | None] = mapped_column(Text, nullable=True, comment="深度链接")
    shifttime: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="时移时间")
    tvod_save_time: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="TVOD保存时间")
    tvod_enable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="TVOD启用")
    tstv_enable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="TSTV启用")
    cutv_enable: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="CUTVE启用")
    encryption: Mapped[bool | None] = mapped_column(Boolean, nullable=True, comment="加密")
    processed_type: Mapped[str] = mapped_column(String(50), nullable=False, comment="操作类型: Add/Update/Delete")
    processed_by: Mapped[str | None] = mapped_column(String(100), nullable=True, comment="操作人")
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
