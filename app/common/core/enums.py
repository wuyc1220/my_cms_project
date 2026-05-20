"""
系统模块枚举定义。

用户、角色、字典等字段的枚举值，禁止在代码中直接使用魔法字符串。
"""

from enum import Enum


class UserStatus(str, Enum):
    """用户状态枚举"""
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETED = "deleted"
    LOCKED = "locked"


class RoleStatus(str, Enum):
    """角色状态枚举"""
    ACTIVE = "active"
    INACTIVE = "inactive"


class DictStatus(str, Enum):
    """字典状态枚举"""
    ACTIVE = "active"
    INACTIVE = "inactive"


class SensitiveWordStatus(str, Enum):
    """敏感词状态枚举"""
    ACTIVE = "active"
    INACTIVE = "inactive"
