"""
内容元数据模块 - 数据模型
"""
from .basic import (
    Tag,
    Genre,
    ContentType,
    PosterSize,
    PosterSizeBelonging,
    PosterSizeExtension,
    Category,
    Cast,
    CustomField,
    CustomFieldBelonging,
    CustomFieldOption,
    Picture,
    EntityFieldValue,
    EntityI18n,
)
from .metadata_enhance import (
    MetadataSource,
    MetadataCrawlTask,
    MetadataCrawlDetail,
)

__all__ = [
    'Tag',
    'Genre',
    'ContentType',
    'PosterSize',
    'PosterSizeBelonging',
    'PosterSizeExtension',
    'Category',
    'Cast',
    'CustomField',
    'CustomFieldBelonging',
    'CustomFieldOption',
    'Picture',
    'EntityFieldValue',
    'EntityI18n',
    'MetadataSource',
    'MetadataCrawlTask',
    'MetadataCrawlDetail',
]