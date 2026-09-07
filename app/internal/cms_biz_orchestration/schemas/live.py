"""
直播管理（Live）Pydantic 数据模型。

覆盖：
- 频道管理（CHANNEL 视角）
- 节目单管理（SCHEDULE 视角）
- 归档管理（已归档 MOVIE/EPISODE/SEASON/SERIES 视角）
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import app_tz
from app.internal.cms_biz_metada.schemas.basic import EntityFieldValueItem


# ─── 频道管理 ─────────────────────────────────────────────────────────

class ChannelListItem(BaseModel):
    """
    频道列表项（直播管理 - 频道管理视角）。

    字段：
        id                内容 id
        title             频道名称
        status            Ingest 状态
        genre_ids         题材 id 列表
        genre_name        题材名称
        channel_number    频道号（来源 ChannelMetadata）
        language          频道语言数组（数据字典 Language code）
        category_names    关联栏目名称列表
        package_names     关联服务包名称列表
        custom_tag_names  自定义标签名称列表
        provider_names    关联供应商名称列表（通过 license 链路）
        license_start     最早许可证开始日期
        license_end       最晚许可证结束日期
        created_at        创建时间
    """
    id: int
    title: str
    status: str
    genre_ids: Optional[list[int]] = None
    genre_name: Optional[str] = None
    channel_number: Optional[int] = None
    language: list[str] = []
    category_names: list[str] = []
    package_names: list[str] = []
    custom_tag_names: list[str] = []
    provider_names: list[str] = []
    license_start: Optional[str] = None
    license_end: Optional[str] = None
    created_at: Optional[datetime] = None
    is_discarded: bool = False

    model_config = ConfigDict(from_attributes=True)


class ChannelDetailItem(BaseModel):
    """
    频道详情响应（直播管理 - 频道详情页）。

    字段：
        id              内容 id
        title           频道名称
        content_type    内容类型（CHANNEL）
        status          Ingest 状态
        genre_ids        题材 id 列表
        genre_name      题材名称
        package_names   关联服务包名称列表
        category_names  关联栏目名称列表
        provider_names  关联供应商名称列表
        license_start   最早许可证开始日期
        license_end     最晚许可证结束日期
        physical_channel_count  物理频道数量
        schedule_count  节目单数量
        created_at      创建时间
        updated_at      更新时间
    """
    id: int
    title: str
    content_type: str = "CHANNEL"
    status: str
    genre_ids: Optional[list[int]] = None
    genre_name: Optional[str] = None
    package_names: list[str] = []
    category_names: list[str] = []
    custom_tag_names: list[str] = []
    provider_names: list[str] = []
    license_start: Optional[str] = None
    license_end: Optional[str] = None
    physical_channel_count: int = 0
    schedule_count: int = 0
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ChannelUpdate(BaseModel):
    """
    编辑频道请求体。

    字段：
        title       频道名称
        genre_ids   题材 id 列表
    """
    title: Optional[str] = Field(None, max_length=100)
    genre_ids: Optional[list[int]] = None


# ─── 物理频道 ─────────────────────────────────────────────────────────────

class PhysicalChannelListItem(BaseModel):
    """
    物理频道列表项。

    字段：
        id              主键
        channel_id      所属业务频道 id
        name            物理频道名称
        channel_number  频道号
        status          状态 YES/NO
        mediaservice    媒体服务
        definition      清晰度
        videoencode     频道编码
        bitrate         频道码率
        deeplink_ch_url 深度链接
        shifttime       时移时间
        tvod_save_time  TVOD保存时间
        tvod_enable     TVOD启用
        tstv_enable     TSTV启用
        cutv_enable     CUTVE启用
        encryption      加密
        created_at      创建时间
        updated_at      更新时间
    """
    id: int
    channel_id: int
    name: Optional[str] = None
    channel_number: Optional[int] = None
    status: bool = True
    mediaservice: Optional[str] = None
    mediaservice_name: Optional[str] = None
    definition: Optional[str] = None
    definition_name: Optional[str] = None
    videoencode: Optional[str] = None
    videoencode_name: Optional[str] = None
    bitrate: Optional[str] = None
    deeplink_ch_url: Optional[str] = None
    shifttime: Optional[int] = None
    tvod_save_time: Optional[int] = None
    tvod_enable: Optional[bool] = None
    tstv_enable: Optional[bool] = None
    cutv_enable: Optional[bool] = None
    encryption: Optional[bool] = None
    field_values: Optional[dict[str, Optional[str]]] = {}
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class PhysicalChannelCreate(BaseModel):
    """
    新增物理频道请求体。

    字段：
        name            物理频道名称
        channel_number  频道号
        status          状态 YES/NO，默认 YES
        mediaservice    媒体服务
        definition      清晰度（必填）
        videoencode     频道编码
        bitrate         频道码率
        deeplink_ch_url 深度链接
        shifttime       时移时间
        tvod_save_time  TVOD保存时间
        tvod_enable     TVOD启用
        tstv_enable     TSTV启用
        cutv_enable     CUTVE启用
        encryption      加密
        custom_fields   自定义字段值列表（可选，新增时一并保存）
    """
    name: Optional[str] = Field(None, max_length=100)
    channel_number: Optional[int] = None
    status: bool = True
    mediaservice: Optional[str] = Field(None, max_length=100)
    definition: Optional[str] = Field(None, max_length=100)
    videoencode: Optional[str] = Field(None, max_length=100)
    bitrate: Optional[str] = Field(None, max_length=100)
    deeplink_ch_url: Optional[str] = Field(None, max_length=200)
    shifttime: Optional[int] = 0
    tvod_save_time: Optional[int] = 0
    tvod_enable: Optional[bool] = False
    tstv_enable: Optional[bool] = False
    cutv_enable: Optional[bool] = False
    encryption: Optional[bool] = True
    custom_fields: Optional[list["EntityFieldValueItem"]] = None


# ─── 物理频道历史记录 ─────────────────────────────────────────────────────

class PhysicalChannelHistoryItem(BaseModel):
    """
    物理频道操作历史记录项。

    字段（按需求文档 3.10）：
        id                  主键
        processed_at        处理时间
        processed_by        处理人
        processed_type      操作类型: Add/Update/Delete
        mediaservice        媒体服务
        definition          清晰度
        videoencode         频道编码
        bitrate             频道码率
    """
    id: int
    processed_at: Optional[datetime] = None
    processed_by: Optional[str] = None
    processed_type: str
    mediaservice: Optional[str] = None
    definition: Optional[str] = None
    videoencode: Optional[str] = None
    bitrate: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PhysicalChannelHistoryQuery(BaseModel):
    """
    物理频道历史记录查询参数。
    """
    processed_type: Optional[str] = None
    processed_by: Optional[str] = None
    page: int = 1
    page_size: int = 10


# ─── 内容-服务包关联 ───────────────────────────────────────────────────────

class ContentPackageRef(BaseModel):
    """
    内容关联的服务包简要信息。

    字段：
        id              服务包 id
        name            服务包名称
        package_type    服务包类型
        allocated_at    关联时间
    """
    id: int
    name: str
    package_type: Optional[str] = None
    allocated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ContentPackageLink(BaseModel):
    """
    内容关联服务包请求体。

    字段：
        package_ids     服务包 id 列表
    """
    package_ids: list[int]


# ─── 内容-栏目关联 ───────────────────────────────────────────────────────────

class ContentCategoryRef(BaseModel):
    """
    内容关联的栏目简要信息。

    字段：
        id              栏目 id
        name            栏目名称
        platform        所属平台
        parent_name     父级栏目名称（如有）
        allocated_at    关联时间
    """
    id: int
    name: str
    platform: str
    parent_name: Optional[str] = None
    allocated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class ContentCategoryLink(BaseModel):
    """
    内容关联栏目请求体。

    字段：
        category_ids    栏目 id 列表
    """
    category_ids: list[int]


# ─── 节目单管理 ───────────────────────────────────────────────────────

class ScheduleListItem(BaseModel):
    """
    节目单列表项（直播管理 - 节目单管理视角）。

    字段：
        id              内容 id
        title           节目名称（Program Name）
        status          Ingest 状态
        channel_id      所属频道 id（parent_id）
        channel_name    所属频道名称（来自 parent 内容标题）
        begin_time      节目开始时间
        end_time        节目结束时间
        cutv_enable     CUTV 启用
        is_archived     是否已归档
        archive_content_id   归档产物 Content ID（可能为空）
        archive_content_type 归档产物类型 MOVIE/EPISODE/SERIES/SEASON
        archive_published    归档产物是否已发布（status=Published）
        archive_scheduled_time 计划归档执行时间（mode=plan 时设置）
        is_published       节目单本身的发布状态（object_publish_status.is_published）
        created_at      创建时间
    """
    id: int
    title: str
    status: str
    channel_id: Optional[int] = None
    channel_name: Optional[str] = None
    begin_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    cutv_enable: Optional[bool] = False
    is_archived: bool = False
    archive_content_id: Optional[int] = None
    archive_content_type: Optional[str] = None
    archive_published: bool = False
    archive_scheduled_time: Optional[datetime] = None
    is_published: bool = False
    created_at: Optional[datetime] = None
    is_discarded: bool = False

    model_config = ConfigDict(from_attributes=True)

    @field_validator("begin_time", "end_time", "archive_scheduled_time", "created_at", mode="after")
    @classmethod
    def _ensure_utc_on_read(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class ScheduleCreate(BaseModel):
    """
    新增节目单请求体。

    字段：
        title       节目名称（Program Name）
        parent_id   所属频道 id（必填，对应 CHANNEL content.id）
        begin_time  开始时间（必填）
        end_time    结束时间（必填）
        assign_to   任务分配用户ID（可选；指定后内容编排任务自动分配给该用户）
    """
    title: str = Field(..., max_length=100)
    parent_id: int
    begin_time: datetime
    end_time: datetime
    assign_to: Optional[int] = None

    @field_validator("begin_time", "end_time", mode="after")
    @classmethod
    def _ensure_timezone(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=app_tz)
        return v


# ─── 归档管理 ─────────────────────────────────────────────────────────

class ArchiveListItem(BaseModel):
    """
    归档内容列表项（直播管理 - 归档管理视角）。

    展示 MOVIE / EPISODE / SEASON / SERIES 类型的已归档内容。
    channel_name / begin_time / end_time 为归档来源播出信息，
    通过 source_schedule_id → SCHEDULE → CHANNEL 获取。

    字段：
        id                内容 id
        content_type      MOVIE/EPISODE/SEASON/SERIES
        title             节目名称
        status            Ingest 状态
        genre_ids         题材 id 列表
        genre_name        题材名称
        type_name         类型名称（来自元数据表的 type_id 关联 ContentType）
        channel_name      归档来源频道名称
        begin_time        播出开始时间
        end_time          播出结束时间
        category_names    关联栏目名称列表
        package_names     关联服务包名称列表
        custom_tag_names  自定义标签名称列表
        provider_names    关联供应商名称列表
        license_start     最早许可证开始日期
        license_end       最晚许可证结束日期
        sequence          集序号（仅 EPISODE 有效）
        series_ordinal    季号（仅 SEASON/SERIES 有效）
        created_at        创建时间
    """
    id: int
    content_type: str
    title: str
    status: str
    genre_ids: Optional[list[int]] = None
    genre_name: Optional[str] = None
    type_name: Optional[str] = None
    channel_name: Optional[str] = None
    begin_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    category_names: list[str] = []
    package_names: list[str] = []
    custom_tag_names: list[str] = []
    provider_names: list[str] = []
    license_start: Optional[str] = None
    license_end: Optional[str] = None
    sequence: Optional[int] = None
    series_ordinal: Optional[int] = None
    created_at: Optional[datetime] = None
    is_discarded: bool = False

    model_config = ConfigDict(from_attributes=True)

    @field_validator("begin_time", "end_time", "created_at", mode="after")
    @classmethod
    def _ensure_utc_on_read(cls, v):
        if isinstance(v, datetime) and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v


class ScheduleImportError(BaseModel):
    """节目单导入行级校验错误。"""
    row: int
    errors: list[str] = []


class ScheduleImportConflict(BaseModel):
    """节目单导入冲突项（频道 + 时间段冲突）。

    conflict_source: existing=与数据库已有节目冲突 / in_file=与本次文件内其他行冲突
    conflict_row:    in_file 时指向文件内冲突的对方行号
    """
    row: int
    channel_name: Optional[str] = None
    title: Optional[str] = None
    begin_time: Optional[str] = None
    end_time: Optional[str] = None
    conflict_ids: list[int] = []
    conflict_source: str = "existing"
    conflict_row: Optional[int] = None


class ScheduleImportResult(BaseModel):
    """节目单批量导入结果。

    字段：
        total       数据总条数
        created     新增数量
        updated     更新数量
        skipped     跳过数量（校验失败）
        conflicts   频道+时间段冲突明细（force=false 时返回；非空代表未提交）
        errors      行级校验错误明细（频道不存在、字典值匹配不上等）
    """
    total: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    conflicts: list[ScheduleImportConflict] = []
    errors: list[ScheduleImportError] = []


# ─── 流程/日志 ───────────────────────────────────────────────────────────

class ProcessListItem(BaseModel):
    """
    流程列表项。

    字段：
        id              流程 id
        name            流程名称
        process_type    流程类型
        status          流程状态
        start_dt        开始时间
        end_dt          结束时间
        assigned        分配人
        processed_before 前置处理
        info            信息
    """
    id: int
    name: str
    node_code: Optional[str] = None
    process_type: Optional[str] = None
    status: Optional[str] = None
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None
    assigned: Optional[str] = None
    assigned_display_name: Optional[str] = None
    processed_before: Optional[bool] = None
    info: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class StatusLogListItem(BaseModel):
    id: int
    processed_at: Optional[datetime] = None
    processed_by: Optional[str] = None
    processed_by_display_name: Optional[str] = None
    before_status: Optional[str] = None
    after_status: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class ActivityLogListItem(BaseModel):
    id: int
    processed_at: Optional[datetime] = None
    processed_by: Optional[str] = None
    processed_by_display_name: Optional[str] = None
    processed_type: Optional[str] = None
    details: Optional[str] = None
    previous_value: Optional[str] = None
    updated_value: Optional[str] = None
    updated_value_json: Optional[str] = None
    entity_type: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


# ─── 审核/发布 ───────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    """
    提交审核请求体。

    字段：
        review_type     审核类型（approve/reject）
        issue_types     内容问题类型列表（审核不通过时必填）
        description     审核说明
        review_level    审批级别（L1/L2/L3），用于多级审批
    """
    review_type: str  # approve / reject
    issue_types: Optional[list[str]] = None
    description: Optional[str] = Field(None, max_length=500)
    review_level: Optional[str] = None  # L1 / L2 / L3


class ReviewResponse(BaseModel):
    """
    审核响应。

    字段：
        success         是否成功
        content_id      内容ID
        review_status   审核结果状态
        message         消息
        auto_approved   是否自动通过（免审批）
        final_approved  是否最终通过
        level_required  需要的审批层级数
    """
    success: bool
    content_id: int
    review_status: str
    message: str
    auto_approved: Optional[bool] = None
    final_approved: Optional[bool] = None
    level_required: Optional[int] = None


class PublishPlanRequest(BaseModel):
    """
    设置发布计划请求体。

    字段：
        publish_time    发布时间
        notes           备注
    """
    publish_time: Optional[datetime] = None
    notes: Optional[str] = None


# ─── 归档 ───────────────────────────────────────────────────────────

class ArchiveRequest(BaseModel):
    """
    归档操作请求体。

    字段：
        schedule_id     待归档节目单 ID（必填）
        mode            执行方式：now=立即归档，plan=计划归档
        scheduled_time  计划执行时间（mode=plan 时必填）
    """
    schedule_id: int
    mode: str = "now"  # now / plan
    scheduled_time: Optional[datetime] = None
    # 归档弹窗随传的元数据字段（mode=now 时可选）：
    # 与归档在同一事务内落库，归档校验失败时整体回滚，避免状态被单独修改
    series_type: Optional[int] = None
    series_name: Optional[str] = None
    series_id: Optional[str] = None
    sequence: Optional[int] = None
    series_ordinal: Optional[int] = None
    show_name: Optional[str] = None
    show_id: Optional[str] = None
    program_id: Optional[str] = None
    cutv_enable: Optional[bool] = None


class ArchiveResponse(BaseModel):
    """
    归档操作响应。

    字段：
        success         是否成功
        schedule_id     节目单 ID
        archive_content_id  归档产物内容 ID（MOVIE 或 EPISODE）
        archive_content_type 归档产物内容类型
        message         消息
    """
    success: bool
    schedule_id: int
    archive_content_id: Optional[int] = None
    archive_content_type: Optional[str] = None
    message: str
