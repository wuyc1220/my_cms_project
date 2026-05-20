"""
内容模块枚举定义。

content_type 和 status 等字段的枚举值，禁止在代码中直接使用魔法字符串。
"""

from enum import Enum


class ContentType(str, Enum):
    """内容类型枚举"""
    MOVIE = "MOVIE"
    EPISODE = "EPISODE"
    SERIES = "SERIES"
    SEASON = "SEASON"
    CHANNEL = "CHANNEL"
    SCHEDULE = "SCHEDULE"


class ContentStatus(str, Enum):
    """内容 Ingest 状态枚举"""
    NONE = "None"
    WAITING_FOR_MATERIALS = "WaitingForMaterials"
    PUBLISHED = "Published"
