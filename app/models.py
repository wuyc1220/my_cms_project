"""
ORM 模型聚合文件 — 供 Alembic 发现所有表结构。

本文件仅做导入，无副作用。各业务模块的模型在定义时自动注册到 Base.metadata。
"""

# 系统管理
from app.internal.cms_biz_system.models import (  # noqa: F401
    Config,
    DictNode,
    OperationLog,
    Role,
    SensitiveWord,
    UsageLimit,
    User,
    UserRole,
)

# 基础元数据
from app.internal.cms_biz_metada.models import (  # noqa: F401
    Cast,
    Category,
    ContentType,
    CustomField,
    CustomFieldBelonging,
    CustomFieldOption,
    EntityFieldValue,
    EntityI18n,
    Genre,
    MetadataCrawlDetail,
    MetadataCrawlTask,
    MetadataSource,
    Picture,
    PosterSize,
    PosterSizeBelonging,
    PosterSizeExtension,
    Tag,
)

# CP/SP 与交易
from app.internal.cms_biz_scp.models import (  # noqa: F401
    Contract,
    ContractAttachment,
    ContractPlatform,
    License,
    LicenseContent,
    Provider,
)

# 内容打包与发布
from app.internal.cms_biz_package.models import (  # noqa: F401
    Content,
    ContentCategory,
    ContentPackage,
    ContentStatus,
    Package,
    PackagePlatform,
    PhysicalChannel,
    PhysicalChannelHistory,
    Task,
    TaskHistory,
)

# 内容采编（编排）
from app.internal.cms_biz_orchestration.models import (  # noqa: F401
    CastRoleMap,
    ChannelMetadata,
    ContentMetadata,
    ContentProcess,
    ContentStatusLog,
    EpisodeHistory,
    Movie,
    MovieHistory,
    ScheduleMetadata,
    SeriesMetadata,
)
