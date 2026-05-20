"""
CP/SP管理模块 - 数据模型
"""
from .trade import (
    Provider,
    Contract,
    ContractAttachment,
    ContractPlatform,
    License,
    LicenseContent,
)

__all__ = [
    'Provider',
    'Contract',
    'ContractAttachment',
    'ContractPlatform',
    'License',
    'LicenseContent',
]