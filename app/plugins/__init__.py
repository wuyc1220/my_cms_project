"""
CMS 插件系统 — 钩子接口定义（hookspec）

所有插件必须遵循此接口契约。CMS 系统通过 pm.hook 调用，
插件通过 @hookimpl 装饰器实现具体逻辑。
"""

from typing import Any


class CrawlerHookSpec:
    """
    元数据爬虫钩子规范

    插件需实现以下钩子方法，CMS 系统在爬取流程中自动调用。
    """

    def crawler_name(self) -> str:
        """
        返回爬虫插件名称（唯一标识）

        Returns:
            爬虫名称，如 "imdb"、"douban"
        """

    def crawler_supported_sources(self) -> list[str]:
        """
        返回此爬虫支持的数据源名称列表（小写匹配）

        Returns:
            数据源名称列表，如 ["imdb"]
        """

    def crawler_supported_types(self) -> list[str]:
        """
        返回此爬虫支持的对象类型列表

        Returns:
            类型列表，如 ["Movie", "Series", "Cast"]
        """

    async def crawler_search(
        self,
        source_url: str,
        query: str,
        object_type: str,
    ) -> list[dict[str, Any]]:
        """
        搜索元数据

        Args:
            source_url: 数据源基础 URL
            query: 搜索关键词
            object_type: 对象类型 Movie/Series/Cast

        Returns:
            搜索结果列表 [{id, title, year}, ...]
        """

    async def crawler_get_detail(
        self,
        source_url: str,
        external_id: str,
        object_type: str,
    ) -> dict[str, Any] | None:
        """
        获取详情数据

        Args:
            source_url: 数据源基础 URL
            external_id: 外部 ID（如 tt1375666）
            object_type: 对象类型

        Returns:
            详情数据字典，字段由各爬虫自定义
        """

    def crawler_map_fields(
        self,
        raw_data: dict[str, Any],
        object_type: str,
        requested_field_codes: list[dict[str, str]],
    ) -> list[dict[str, Any]]:
        """
        将爬虫原始数据映射为 CMS 字段候选值

        Args:
            raw_data: crawler_get_detail 返回的原始数据
            object_type: 对象类型
            requested_field_codes: 前端请求的字段列表 [{"code": "title", "name": "标题"}, ...]

        Returns:
            映射后的候选值列表 [{field_code, field_name, crawl_data}, ...]
        """

    async def crawler_close(self) -> None:
        """
        关闭爬虫，释放资源（浏览器、连接池等）

        在应用关闭或插件卸载时调用
        """
