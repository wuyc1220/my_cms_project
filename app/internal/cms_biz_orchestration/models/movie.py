"""
媒资实体模型。

存储 Program（MOVIE/EPISODE）的实际媒体文件信息，
包括正片、预告片、字幕三种类型。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, SmallInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Movie(Base):
    """
    媒资实体表。

    字段：
        id              主键
        content_id      所属内容 ID（MOVIE 或 EPISODE）
        file_name       文件名
        file_path       文件存储路径
        file_size       文件大小（字节）
        movie_type      媒资类型：1=正片，2=预告片，3=字幕
        audio_type      音频类型（来源数据字典）
        screen_format   画面格式：0=4x3，1=16x9 Wide，4K
        closed_captioning   隐藏字幕
        duration        时长（分钟），非字幕必填
        definition      清晰度：SD/HD，非字幕必填
        encryption      加密
        publish_flag    发布标识
        deeplink        深度链接
        created_at      创建时间

    约束：
        - 同一 content_id 下可存在多个 movie 记录（正片+预告片+字幕）
        - movie_type + content_id 不唯一，允许同一类型多个文件
    """

    __tablename__ = "movie"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="所属内容 ID，FK → content.id（MOVIE 或 EPISODE）",
    )

    # ── 文件信息 ───────────────────────────────────────────
    file_name: Mapped[str] = mapped_column(String(500), nullable=False, comment="文件名")
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False, comment="文件存储路径（本地/SFTP/MinIO）")
    relative_path: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="文件相对路径，用于下载")
    file_size: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0", comment="文件大小（字节）"
    )

    # ── 媒资属性 ───────────────────────────────────────────
    movie_type: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, comment="媒资类型: 1=Movie, 2=Trailer, 3=Subtitle"
    )
    audio_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="音频类型，来源数据字典"
    )
    screen_format: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="画面格式: 0=4x3, 1=16x9 Wide, 4K"
    )
    closed_captioning: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="隐藏字幕"
    )
    duration: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="时长（分钟），非字幕类型必填，字幕类型可为空"
    )
    definition: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="清晰度: SD/HD，非字幕类型必填，字幕类型可为空"
    )
    encryption: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="加密"
    )
    publish_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="发布标识"
    )
    deeplink: Mapped[str | None] = mapped_column(
        String(1000), nullable=True, comment="深度链接"
    )
    ingest_status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="None", server_default="None", comment="Ingest 状态：None/success/failure/processing"
    )

    # ── 材料专属字段 ───────────────────────────────────────────
    sequence: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="序号（预告片排序用）",
    )
    mediaservice: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="媒体服务，来源数据字典 mediaservice"
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
