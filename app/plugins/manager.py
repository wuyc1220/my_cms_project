"""
CMS 插件系统 — 插件管理器

负责注册、调度、生命周期管理。
CMS 系统通过 pm.hook.crawl_xxx() 触发调用，
所有已注册的爬虫插件自动执行。
"""

import pluggy
from loguru import logger

from app.plugins.hooks import CrawlerSpec

_pm: pluggy.PluginManager | None = None


def get_plugin_manager() -> pluggy.PluginManager:
    """获取全局插件管理器单例"""
    global _pm
    if _pm is None:
        _pm = create_plugin_manager()
    return _pm


def create_plugin_manager() -> pluggy.PluginManager:
    """创建并初始化插件管理器"""
    pm = pluggy.PluginManager("cms_crawler")
    pm.add_hookspecs(CrawlerSpec)

    from app.plugins.imdb_plugin import IMDbCrawlerPlugin
    pm.register(IMDbCrawlerPlugin(), name="imdb_crawler")

    registered = pm.get_plugins()
    logger.info("CMS 爬虫插件管理器初始化完成，已注册插件: {}", [pm.get_name(p) or p.__class__.__name__ for p in registered])

    return pm


async def shutdown_plugins() -> None:
    """关闭所有插件，释放资源"""
    pm = get_plugin_manager()
    try:
        for plugin in pm.get_plugins():
            try:
                if hasattr(plugin, 'crawler_close'):
                    await plugin.crawler_close()
            except Exception as e:
                logger.error("关闭插件 {} 异常: {}", pm.get_name(plugin), str(e))
        logger.info("CMS 爬虫插件已全部关闭")
    except Exception as e:
        logger.error("关闭爬虫插件异常: {}", str(e))


def find_crawler_for_source(pm: pluggy.PluginManager, source_name: str) -> object | None:
    """
    根据数据源名称查找匹配的爬虫插件

    Args:
        pm: 插件管理器
        source_name: 数据源名称

    Returns:
        匹配的插件实例，未找到返回 None
    """
    source_lower = source_name.lower()
    for plugin in pm.get_plugins():
        results = pm.hook.crawler_supported_sources(plugin=plugin)
        if results:
            supported = results[0] if isinstance(results[0], list) else results
            if source_lower in [s.lower() for s in supported]:
                return plugin
    return None
