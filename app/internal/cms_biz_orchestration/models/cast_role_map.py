"""
人物角色关联模型。

存储节目/影片与 Cast（人物）之间的关联关系，以及该人物的角色。
"""

from datetime import datetime

from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

if TYPE_CHECKING:
    from app.internal.cms_biz_metada.models.basic import Cast


class CastRoleMap(Base):
    """
    人物角色关联表（content_cast_role_map）。

    字段：
        map_id      主键
        program_id  关联节目主表
        movie_id    关联单集/影片表
        content_id  关联内容主表
        cast_id     关联人员表
        role_name   角色名称
        role_code   角色编码
        is_deleted  软删除标记
        created_at  创建时间
        updated_at  更新时间
    """

    __tablename__ = "content_cast_role_map"

    map_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="自增主键ID"
    )
    program_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="关联节目主表"
    )
    movie_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="关联单集/影片表"
    )
    content_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True, comment="关联内容主表"
    )
    cast_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("cast.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="关联人员表cast_id",
    )
    cast: Mapped["Cast"] = relationship(
        "Cast",
        foreign_keys=[cast_id],
        lazy="raise",
    )
    role_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="角色名称"
    )
    role_code: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="角色编码"
    )

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
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
