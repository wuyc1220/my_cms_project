"""
C2 规范 ADI XML 生成模块。

对外暴露主入口 :class:`ADIBuilder`，串联 CMS 业务数据到标准 ADI XML。

用法示例::

    from app.soap.c2 import ADIBuilder

    async with async_session() as db:
        builder = ADIBuilder(db)
        xml_str = await builder.build_publish_xml(content_id=123)
"""
from .builder import ADIBuilder
from .category_builder import CategorySyncBuilder
from .package_builder import PackageSyncBuilder
from .constants import Action, ElementType

__all__ = ["ADIBuilder", "CategorySyncBuilder", "PackageSyncBuilder", "Action", "ElementType"]
