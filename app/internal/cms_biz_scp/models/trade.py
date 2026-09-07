"""
供应链（Trade）领域模型。

包含：
- Provider：供应商主表
- Contract：合同主表
- ContractPlatform：合同平台关联（一合同多平台，含商业权利标记）
- ContractAttachment：合同附件
- License：许可证主表
- LicensePlatform：许可证平台关联（含广告权利标记）
- LicenseContent：许可证与内容的多对多关联
"""

from datetime import date, datetime, time

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Provider(Base):
    """
    供应商主表。

    字段：
        id                  主键
        provider_code       供应商编码，全局唯一，不填则自动生成
        name                供应商名称，全局唯一
        country             国家/地区
        review_level        内容审核层级（来源数据字典 Content_review_level）
        l1_assignee_id      L1 审核默认分配人（FK → user.id）
        l2_assignee_id      L2 审核默认分配人（FK → user.id）
        l3_assignee_id      L3 审核默认分配人（FK → user.id）
        notes               备注
        is_deleted          软删除标记
        created_at          创建时间
        updated_at          更新时间

    关系：
        contracts           → Contract，该供应商下的所有合同
    """

    __tablename__ = "provider"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider_code: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True, comment="供应商编码，全局唯一，不填则自动生成"
    )
    name: Mapped[str] = mapped_column(
        String(200), nullable=False, index=True, comment="供应商名称，全局唯一"
    )
    country: Mapped[str | None] = mapped_column(String(200), nullable=True, comment="国家/地区")
    review_level: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="内容审核层级，来源数据字典 Content_review_level"
    )
    l1_assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="L1 审核默认分配人"
    )
    l2_assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="L2 审核默认分配人"
    )
    l3_assignee_id: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="L3 审核默认分配人"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    contracts: Mapped[list["Contract"]] = relationship(
        "Contract", back_populates="provider", lazy="select"
    )


class Contract(Base):
    """
    合同主表。

    字段：
        id              主键
        name            合同名称，全局唯一
        provider_id     所属供应商（FK → provider.id）
        start_date      合同开始日期
        end_date        合同结束日期
        notes           备注
        is_deleted      软删除标记
        created_at      创建时间
        updated_at      更新时间

    关系：
        provider        → Provider
        platforms       → ContractPlatform，关联的平台及商业权利
        attachments     → ContractAttachment，合同附件
        licenses        → License，该合同下的所有许可证
    """

    __tablename__ = "contract"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(500), nullable=False, index=True, comment="合同名称，全局唯一"
    )
    provider_id: Mapped[int] = mapped_column(
        ForeignKey("provider.id", ondelete="RESTRICT"), nullable=False, index=True,
        comment="所属供应商"
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="合同开始日期")
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="合同结束日期")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    provider: Mapped["Provider"] = relationship("Provider", back_populates="contracts", lazy="selectin")
    platforms: Mapped[list["ContractPlatform"]] = relationship(
        "ContractPlatform", cascade="all, delete-orphan", lazy="selectin"
    )
    attachments: Mapped[list["ContractAttachment"]] = relationship(
        "ContractAttachment", cascade="all, delete-orphan", lazy="select"
    )
    licenses: Mapped[list["License"]] = relationship(
        "License", back_populates="contract", lazy="select"
    )


class ContractPlatform(Base):
    """
    合同平台关联表（一合同多平台）。

    字段：
        contract_id         外键 → contract.id
        platform            平台值（来源数据字典 Platform）
        commercial_rights   是否具有商业权利
    """

    __tablename__ = "contract_platform"

    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contract.id", ondelete="CASCADE"), primary_key=True
    )
    platform: Mapped[str] = mapped_column(String(50), primary_key=True)
    commercial_rights: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="是否具有商业权利"
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class ContractAttachment(Base):
    """
    合同附件表。

    字段：
        id              主键
        contract_id     外键 → contract.id
        file_name       原始文件名
        file_path       存储路径（SFTP 路径）
        file_size       文件大小（字节）
        uploaded_by     上传人（FK → user.id）
        created_at      上传时间
    """

    __tablename__ = "contract_attachment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contract_id: Mapped[int] = mapped_column(
        ForeignKey("contract.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file_name: Mapped[str] = mapped_column(String(500), nullable=False, comment="原始文件名")
    file_path: Mapped[str] = mapped_column(String(1000), nullable=False, comment="存储路径")
    relative_path: Mapped[str | None] = mapped_column(String(1000), nullable=True, comment="文件相对路径，用于下载")
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="文件大小（字节）")
    uploaded_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class License(Base):
    """
    许可证主表。

    字段：
        id                  主键
        name                许可证名称，全局唯一
        contract_id         所属合同（FK → contract.id）
        service_type        服务类型（来源数据字典 ServiceType）
        regions             授权地区，逗号分隔（来源数据字典 Regions）
        start_date          授权开始日期
        end_date            授权结束日期
        status              状态：ACTIVE/INACTIVE/EXPIRED/DELETED（系统自动维护）
        mobile_download     是否允许移动端下载
        download_duration   下载有效天数（mobile_download=True 时有效）
        mobile_preview      是否允许移动端预览
        preview_begin_time  预览开始时间（mobile_preview=True 时有效）
        preview_end_time    预览结束时间（mobile_preview=True 时有效）
        notes               备注
        is_deleted          软删除标记
        created_at          创建时间
        updated_at          更新时间

    关系：
        contract            → Contract
        platforms           → LicensePlatform，关联的平台及广告权利
        license_contents    → LicenseContent，关联内容中间表
    """

    __tablename__ = "license"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(
        String(500), nullable=False, index=True, comment="许可证名称，全局唯一"
    )
    contract_id: Mapped[int | None] = mapped_column(
        ForeignKey("contract.id", ondelete="RESTRICT"), nullable=True, index=True,
        comment="所属合同"
    )
    service_type: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="服务类型，来源数据字典 ServiceType"
    )
    regions: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="授权地区，逗号分隔（来源数据字典 Regions）"
    )
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="授权开始日期")
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True, comment="授权结束日期")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ACTIVE", server_default="ACTIVE",
        comment="状态：ACTIVE/INACTIVE/EXPIRED/DELETED，系统维护"
    )
    mobile_download: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="是否允许移动端下载"
    )
    download_duration: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="下载有效天数"
    )
    mobile_preview: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="是否允许移动端预览"
    )
    preview_begin_time: Mapped[time | None] = mapped_column(
        Time, nullable=True, comment="预览开始时间"
    )
    preview_end_time: Mapped[time | None] = mapped_column(
        Time, nullable=True, comment="预览结束时间"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True, comment="备注")
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)

    contract: Mapped["Contract"] = relationship("Contract", back_populates="licenses", lazy="selectin")
    platforms: Mapped[list["LicensePlatform"]] = relationship(
        "LicensePlatform", cascade="all, delete-orphan", lazy="selectin"
    )
    license_contents: Mapped[list["LicenseContent"]] = relationship(
        "LicenseContent", cascade="save-update, merge", lazy="select"
    )


class LicensePlatform(Base):
    """
    许可证平台关联表（一许可证多平台）。

    字段：
        license_id      外键 → license.id
        platform        平台值（来源数据字典 Platform）
        ad_rights       是否具有广告权利
    """

    __tablename__ = "license_platform"

    license_id: Mapped[int] = mapped_column(
        ForeignKey("license.id", ondelete="CASCADE"), primary_key=True
    )
    platform: Mapped[str] = mapped_column(String(50), primary_key=True)
    ad_rights: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
        comment="是否具有广告权利"
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)


class LicenseContent(Base):
    """
    许可证与内容的多对多关联表。

    字段：
        id          主键（自增，便于前端 rowKey）
        license_id  外键 → license.id
        content_id  外键 → content.id
        created_at  关联创建时间
    """

    __tablename__ = "license_content"
    __table_args__ = (
        UniqueConstraint("license_id", "content_id", name="uq_license_content"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    license_id: Mapped[int] = mapped_column(
        ForeignKey("license.id", ondelete="CASCADE"), nullable=False, index=True
    )
    content_id: Mapped[int] = mapped_column(
        ForeignKey("content.id", ondelete="CASCADE"), nullable=False, index=True
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    created_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True)
