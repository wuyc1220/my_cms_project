"""
供应链（Trade）Pydantic 数据模型。

包含：
- ProviderListItem / ProviderCreate / ProviderUpdate
- ContractPlatformItem / ContractListItem / ContractCreate / ContractUpdate
- ContractAttachmentItem
- LicensePlatformItem / LicenseListItem / LicenseCreate / LicenseUpdate
- LicenseContentItem
- ContentForTradeItem  （交易模块内容搜索结果）
- ContentAddToLicenseRequest
"""

from datetime import date, datetime, time

from pydantic import BaseModel, ConfigDict, Field


# ─── Provider ─────────────────────────────────────────────────────────

class ProviderListItem(BaseModel):
    """供应商列表项 / 详情响应。"""
    id: int
    provider_code: str | None = None
    name: str
    country: str | None = None
    review_level: str | None = None
    l1_assignee_id: int | None = None
    l2_assignee_id: int | None = None
    l3_assignee_id: int | None = None
    l1_assignee_name: str | None = None
    l2_assignee_name: str | None = None
    l3_assignee_name: str | None = None
    notes: str | None = None
    contract_count: int = 0
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ProviderCreate(BaseModel):
    """新建供应商请求体。必填：name"""
    provider_code: str | None = Field(None, max_length=100)
    name: str = Field(..., max_length=100)
    country: str | None = Field(None, max_length=100)
    review_level: str | None = None
    l1_assignee_id: int | None = None
    l2_assignee_id: int | None = None
    l3_assignee_id: int | None = None
    notes: str | None = Field(None, max_length=500)


class ProviderUpdate(BaseModel):
    """编辑供应商请求体，所有字段均可选。"""
    provider_code: str | None = Field(None, max_length=100)
    name: str | None = Field(None, max_length=100)
    country: str | None = Field(None, max_length=100)
    review_level: str | None = None
    l1_assignee_id: int | None = None
    l2_assignee_id: int | None = None
    l3_assignee_id: int | None = None
    notes: str | None = Field(None, max_length=500)


class ProviderSimpleItem(BaseModel):
    """供应商简要信息，用于下拉选择。"""
    id: int
    provider_code: str | None = None
    name: str

    model_config = ConfigDict(from_attributes=True)


class ProviderHistoryItem(BaseModel):
    """
    供应商操作历史项（Processed History）。

    字段映射 operation_log 表：
        id              日志 id
        processed_at    操作时间（operation_time）
        processed_by    操作人（user_name）
        processed_type  操作类型（operation_type，形如"供应商创建"）
        details         详细内容（operation_content）
        previous_value  操作前的实体快照（JSON）
        updated_value   操作后的实体快照（JSON）
    """
    id: int
    processed_at: datetime | None = None
    processed_by: str | None = None
    processed_type: str | None = None
    details: str | None = None
    previous_value: str | None = None
    updated_value: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ─── Contract ─────────────────────────────────────────────────────────

class ContractPlatformItem(BaseModel):
    """合同平台条目（含商业权利标记）。"""
    platform: str
    commercial_rights: bool = False

    model_config = ConfigDict(from_attributes=True)


class ContractListItem(BaseModel):
    """合同列表项 / 详情响应。"""
    id: int
    name: str
    provider_id: int
    provider_name: str
    platforms: list[ContractPlatformItem] = []
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = None
    license_count: int = 0
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ContractCreate(BaseModel):
    """新建合同请求体。必填：name、provider_id、start_date、end_date"""
    name: str = Field(..., max_length=100)
    provider_id: int
    platforms: list[ContractPlatformItem] = []
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = Field(None, max_length=500)


class ContractUpdate(BaseModel):
    """编辑合同请求体，所有字段均可选。"""
    name: str | None = Field(None, max_length=100)
    provider_id: int | None = None
    platforms: list[ContractPlatformItem] | None = None
    start_date: date | None = None
    end_date: date | None = None
    notes: str | None = Field(None, max_length=500)


class ContractSimpleItem(BaseModel):
    """合同简要信息，用于下拉选择。"""
    id: int
    name: str
    provider_id: int
    provider_name: str

    model_config = ConfigDict(from_attributes=True)


class ContractAttachmentItem(BaseModel):
    """合同附件信息。"""
    id: int
    contract_id: int
    file_name: str
    file_path: str
    file_size: int | None = None
    uploaded_by: int | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ContractHistoryItem(BaseModel):
    """
    合同操作历史项（Processed History）。

    字段映射 operation_log 表：
        id              日志 id
        processed_at    操作时间（operation_time）
        processed_by    操作人（user_name）
        processed_type  操作类型（operation_type，形如"合同创建"）
        details         详细内容（operation_content）
        previous_value  操作前的实体快照（JSON）
        updated_value   操作后的实体快照（JSON）
    """
    id: int
    processed_at: datetime | None = None
    processed_by: str | None = None
    processed_type: str | None = None
    details: str | None = None
    previous_value: str | None = None
    updated_value: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ─── License ──────────────────────────────────────────────────────────

class LicensePlatformItem(BaseModel):
    """许可证平台条目（含广告权利标记）。"""
    platform: str
    ad_rights: bool = False

    model_config = ConfigDict(from_attributes=True)


class LicenseListItem(BaseModel):
    """许可证列表项 / 详情响应。"""
    id: int
    name: str
    contract_id: int | None
    contract_name: str
    provider_id: int
    provider_name: str
    service_type: str
    platforms: list[LicensePlatformItem] = []
    regions: list[str] = []
    start_date: date | None = None
    end_date: date | None = None
    status: str = "ACTIVE"
    mobile_download: bool = False
    download_duration: int | None = None
    mobile_preview: bool = False
    preview_begin_time: time | None = None
    preview_end_time: time | None = None
    notes: str | None = None
    content_count: int = 0
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class LicenseCreate(BaseModel):
    """新建许可证请求体。必填：name、service_type、start_date、end_date"""
    name: str = Field(..., max_length=100)
    contract_id: int | None = None
    service_type: str
    platforms: list[LicensePlatformItem] = []
    regions: list[str] = []
    start_date: date
    end_date: date
    mobile_download: bool = False
    download_duration: int | None = None
    mobile_preview: bool = False
    preview_begin_time: time | None = None
    preview_end_time: time | None = None
    notes: str | None = Field(None, max_length=500)


class LicenseUpdate(BaseModel):
    """编辑许可证请求体，所有字段均可选。"""
    name: str | None = Field(None, max_length=100)
    contract_id: int | None = None
    unlink_contract: bool = False
    service_type: str | None = None
    platforms: list[LicensePlatformItem] | None = None
    regions: list[str] | None = None
    start_date: date | None = None
    end_date: date | None = None
    mobile_download: bool | None = None
    download_duration: int | None = None
    mobile_preview: bool | None = None
    preview_begin_time: time | None = None
    preview_end_time: time | None = None
    notes: str | None = Field(None, max_length=500)


class LicenseSimpleItem(BaseModel):
    """许可证简要信息，用于合同"添加内容"弹框的中栏列表。"""
    id: int
    name: str
    contract_id: int | None
    service_type: str
    start_date: date | None = None
    end_date: date | None = None

    model_config = ConfigDict(from_attributes=True)


class LicenseHistoryItem(BaseModel):
    """
    许可证操作历史项（Processed History）。

    字段映射 operation_log 表：
        id              日志 id
        processed_at    操作时间（operation_time）
        processed_by    操作人（user_name）
        processed_type  操作类型（operation_type，形如“许可证创建”）
        details         详细内容（operation_content）
        previous_value  操作前的实体快照（JSON）
        updated_value   操作后的实体快照（JSON）
    """
    id: int
    processed_at: datetime | None = None
    processed_by: str | None = None
    processed_type: str | None = None
    details: str | None = None
    previous_value: str | None = None
    updated_value: str | None = None

    model_config = ConfigDict(from_attributes=True)


# ─── Content（交易模块视角）────────────────────────────────────────────

class ContentForTradeItem(BaseModel):
    """交易模块中的内容简要信息。"""
    id: int
    content_type: str
    title: str
    status: str
    license_names: list[str] = []
    genre: str | None = None
    original_name: str | None = None
    release_year: int | None = None

    model_config = ConfigDict(from_attributes=True)


class ContentAddToLicenseRequest(BaseModel):
    """向许可证添加内容的请求体。"""
    content_ids: list[int]
