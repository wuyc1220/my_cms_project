"""
CMS 插件系统 — 钩子规范定义

基于 pluggy 的 hookspec，定义元数据爬虫的接口契约。
所有爬虫插件必须实现这些钩子。
"""

import pluggy

hookspec = pluggy.HookspecMarker("cms_crawler")
hookimpl = pluggy.HookimplMarker("cms_crawler")


class CrawlerSpec:
    """元数据爬虫钩子规范"""

    @hookspec
    def crawler_name(self) -> str:
        """返回爬虫插件名称（唯一标识），如 'imdb'、'douban'"""

    @hookspec
    def crawler_supported_sources(self) -> list[str]:
        """返回此爬虫支持的数据源名称列表（小写匹配），如 ['imdb']"""

    @hookspec
    def crawler_supported_types(self) -> list[str]:
        """返回此爬虫支持的对象类型列表，如 ['Movie', 'Series', 'Cast']"""

    @hookspec(firstresult=True)
    async def crawler_search(
        self,
        source_url: str,
        query: str,
        object_type: str,
    ) -> list[dict]:
        """
        搜索元数据

        Args:
            source_url: 数据源基础 URL
            query: 搜索关键词
            object_type: 对象类型 Movie/Series/Cast

        Returns:
            搜索结果列表 [{id, title, year}, ...]
        """

    @hookspec(firstresult=True)
    async def crawler_get_detail(
        self,
        source_url: str,
        external_id: str,
        object_type: str,
    ) -> dict | None:
        """
        获取详情数据

        Args:
            source_url: 数据源基础 URL
            external_id: 外部 ID（如 tt1375666）
            object_type: 对象类型

        Returns:
            详情数据字典
        """

    @hookspec(firstresult=True)
    def crawler_map_fields(
        self,
        raw_data: dict,
        object_type: str,
        requested_field_codes: list[dict],
    ) -> list[dict]:
        """
        将爬虫原始数据映射为 CMS 字段候选值

        Args:
            raw_data: crawler_get_detail 返回的原始数据
            object_type: 对象类型
            requested_field_codes: 前端请求的字段列表

        Returns:
            映射后的候选值列表 [{field_code, field_name, crawl_data}, ...]
        """

    @hookspec
    async def crawler_close(self) -> None:
        """关闭爬虫，释放资源"""
