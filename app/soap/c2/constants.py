"""
C2 规范常量定义。

本文件集中定义 ADI XML 涉及的：
- ElementType 枚举（11 种对象类型）
- Action 枚举（REGIST/UPDATE/DELETE 三态）
- EntityType → ElementType 映射（Picture 多态表使用）
- ContentType → ElementType 映射（Content 表 content_type 分发）
- 合法的 Mapping 父子组合矩阵
"""
from enum import Enum


# ═══════════════════════════════════════════════════════════
# 1. ElementType — 对象类型枚举
# ═══════════════════════════════════════════════════════════
class ElementType(str, Enum):
    """
    C2 规范 Object ElementType，共 11 种。

    与 CMS 模型的对应关系：
        PROGRAM          → Content (content_type = MOVIE / EPISODE) + ContentMetadata
        MOVIE            → Movie
        SERIES           → Content (content_type = SERIES / SEASON_SERIES / SEASON) + SeriesMetadata
        SCHEDULE         → Content (content_type = SCHEDULE) + ScheduleMetadata
        CHANNEL          → Content (content_type = CHANNEL) + ChannelMetadata
        PHYSICAL_CHANNEL → PhysicalChannel
        CATEGORY         → Category
        CAST             → Cast
        CAST_ROLE_MAP    → CastRoleMap
        PICTURE          → Picture
        PACKAGE          → Package
    """

    PROGRAM = "Program"
    MOVIE = "Movie"
    SERIES = "Series"
    SCHEDULE = "Schedule"
    CHANNEL = "Channel"
    PHYSICAL_CHANNEL = "PhysicalChannel"
    CATEGORY = "Category"
    CAST = "Cast"
    CAST_ROLE_MAP = "CastRoleMap"
    PICTURE = "Picture"
    PACKAGE = "Package"


# ═══════════════════════════════════════════════════════════
# 2. Action — 操作类型枚举
# ═══════════════════════════════════════════════════════════
class Action(str, Enum):
    """
    C2 规范 Object Action，共 2 种。

    REGIST: 全量注入，携带完整属性；下游根据 ID 是否存在自行判断新增/更新
    DELETE: 删除对象，仅需 ElementType + ID；LSP 会级联删除相关 Mapping
    """

    REGIST = "REGIST"
    DELETE = "DELETE"


# ═══════════════════════════════════════════════════════════
# 3. Content.content_type → ElementType 映射
# ═══════════════════════════════════════════════════════════
# Content 是"合并表"，通过 content_type 区分不同 C2 对象类型
CONTENT_TYPE_TO_ELEMENT: dict[str, ElementType] = {
    "MOVIE": ElementType.PROGRAM,
    "EPISODE": ElementType.PROGRAM,
    "SERIES": ElementType.SERIES,
    "SEASON_SERIES": ElementType.SERIES,
    "SEASON": ElementType.SERIES,
    "CHANNEL": ElementType.CHANNEL,
    "SCHEDULE": ElementType.SCHEDULE,
}


# ═══════════════════════════════════════════════════════════
# 4. Picture.entity_type → ElementType 映射
# ═══════════════════════════════════════════════════════════
# Picture 是多态表，通过 entity_type 指向不同的业务实体
PICTURE_ENTITY_TYPE_TO_ELEMENT: dict[str, ElementType] = {
    "category": ElementType.CATEGORY,
    "cast": ElementType.CAST,
    "program": ElementType.PROGRAM,
    "series": ElementType.SERIES,
    "season_series": ElementType.SERIES,
    "channel": ElementType.CHANNEL,
    "schedule": ElementType.SCHEDULE,
    "movie": ElementType.PROGRAM,
    "episode": ElementType.PROGRAM,
    "season": ElementType.SERIES,
}


# ═══════════════════════════════════════════════════════════
# 5. 合法 Mapping 父子组合矩阵（15 种，严格遵循 C2 规范）
# ═══════════════════════════════════════════════════════════
# 用于 Mapping 构建时的合法性校验；违反则静默丢弃并记录告警。
VALID_MAPPINGS: set[tuple[ElementType, ElementType]] = {
    # Category 作为父
    (ElementType.CATEGORY, ElementType.PROGRAM),
    (ElementType.CATEGORY, ElementType.SERIES),
    (ElementType.CATEGORY, ElementType.CHANNEL),
    # Package 作为父
    (ElementType.PACKAGE, ElementType.PROGRAM),
    (ElementType.PACKAGE, ElementType.SERIES),
    (ElementType.PACKAGE, ElementType.CHANNEL),
    # Series 作为父
    (ElementType.SERIES, ElementType.PROGRAM),
    (ElementType.SERIES, ElementType.MOVIE),
    (ElementType.SERIES, ElementType.CAST_ROLE_MAP),
    # Program 作为父
    (ElementType.PROGRAM, ElementType.MOVIE),
    (ElementType.PROGRAM, ElementType.CAST_ROLE_MAP),
    # Picture 作为父
    (ElementType.PICTURE, ElementType.CATEGORY),
    (ElementType.PICTURE, ElementType.PROGRAM),
    (ElementType.PICTURE, ElementType.SERIES),
    (ElementType.PICTURE, ElementType.CAST),
}


# ═══════════════════════════════════════════════════════════
# 6. Ingest 状态枚举（用于注入策略判定）
# ═══════════════════════════════════════════════════════════
INGEST_STATUS_PUBLISHED = "Published"  # 已成功注入
INGEST_STATUS_NONE = "None"            # 未注入
INGEST_STATUS_WAITING = "WaitingForMaterials"  # 等待素材
INGEST_STATUS_SUCCESS = "success"      # Category/Package/Cast 的成功状态
INGEST_STATUS_FAILURE = "failure"      # Category/Package/Cast 的失败状态
