"""SOAP 内容分发配置管理"""
from pydantic_settings import BaseSettings
from typing import Optional


class SOAPSettings(BaseSettings):
    """SOAP 内容分发配置
    
    注意：以下配置项可在数据库 config 表中动态覆盖：
    - SOAP_ENABLED
    - SOAP_CSP_ID, SOAP_LSP_ID, SOAP_LSP_SOAP_URL
    - SOAP_TIMEOUT, SOAP_MAX_RETRIES, SOAP_RETRY_DELAY
    """
    
    # 是否启用 SOAP 内容分发
    enabled: bool = False
    
    # CSP 配置（本系统标识）
    csp_id: str = "SAAT-CMS-001"
    
    # LSP 配置（对接方标识）
    lsp_id: str = "ZTE-MW-001"
    
    # LSP SOAP 服务端地址
    lsp_soap_url: str = "http://lsp-server.com/soap/wsdl"

    # XML 指令文件 FTP/文件路径前缀（文件直传模式，LSP 通过 FTP/SFTP 路径直接访问）
    # 格式: ftp://user:password@host:port/base_path
    # 示例: ftp://zxin10:os10+ZTE@80.80.134.134:21/home/zxin10/cms
    # 最终 CmdFileURL = {cmd_file_url_prefix}/{filepath}
    cmd_file_url_prefix: str = ""

    # CMS 提供给 LSP 的回调地址（结果通知接口 - JSON 格式）
    # LSP 执行完指令后调用此地址通知执行结果
    # 格式: http://<CMS公网地址>/soap/result-notify
    result_notify_url: str = "http://cms-server.com/soap/result-notify"

    # CMS 提供给 LSP 的回调地址（结果通知接口 - SOAP XML 格式）
    # 如果 LSP 发 SOAP XML 格式的 ResultNotifyReq，使用此地址
    # 格式: http://<CMS公网地址>/soap/result-notify-xml
    result_notify_xml_url: str = "http://cms-server.com/soap/result-notify-xml"

    # CMS 提供给 LSP 的 XML 文件下载地址
    # LSP 收到 ExecCmdReq 后通过此地址下载 XML 指令文件
    # 格式: http://<CMS公网地址>/commands
    cmd_file_base_url: str = "http://cms-server.com/commands"

    # SOAP 请求超时时间（秒）
    timeout: int = 30

    # 重试配置
    max_retries: int = 3
    retry_delay: int = 5  # 秒

    # SSRF 防护：允许访问私网/保留地址段的结果文件 URL 白名单
    # 逗号分隔的 hostname 或 CIDR（如 "192.168.10.0/24,lsp-internal.com"）
    # 白名单优先于私网拦截；未配置时一律拒绝私网地址
    result_url_private_allowlist: str = ""
    
    class Config:
        env_file = ".env"
        env_prefix = "SOAP_"
        extra = "ignore"  # 忽略 .env 中非 SOAP_ 前缀的变量，避免与全局 settings 冲突


# 全局 SOAP 配置实例
soap_settings = SOAPSettings()


async def load_soap_config_from_db(db) -> None:
    """
    从数据库加载 SOAP 配置并更新全局 soap_settings
    
    在应用启动时调用，优先使用数据库配置覆盖 .env
    """
    from app.internal.cms_biz_system.services.config_service import (
        get_config_value, get_config_bool, get_config_int
    )
    
    try:
        # 加载布尔配置
        soap_settings.enabled = await get_config_bool(db, "SOAP_ENABLED", default_value=soap_settings.enabled)
        
        # 加载字符串配置
        soap_settings.csp_id = await get_config_value(db, "SOAP_CSP_ID", default_value=soap_settings.csp_id)
        soap_settings.lsp_id = await get_config_value(db, "SOAP_LSP_ID", default_value=soap_settings.lsp_id)
        soap_settings.lsp_soap_url = await get_config_value(db, "SOAP_LSP_SOAP_URL", default_value=soap_settings.lsp_soap_url)
        soap_settings.result_url_private_allowlist = await get_config_value(
            db, "SOAP_RESULT_URL_PRIVATE_ALLOWLIST", default_value=soap_settings.result_url_private_allowlist
        )
        
        # 加载整数配置
        soap_settings.timeout = await get_config_int(db, "SOAP_TIMEOUT", default_value=soap_settings.timeout)
        soap_settings.max_retries = await get_config_int(db, "SOAP_MAX_RETRIES", default_value=soap_settings.max_retries)
        soap_settings.retry_delay = await get_config_int(db, "SOAP_RETRY_DELAY", default_value=soap_settings.retry_delay)
        
        from loguru import logger
        logger.info(f"SOAP 配置从数据库加载完成 | enabled={soap_settings.enabled}")
    except Exception as e:
        from loguru import logger
        logger.warning(f"从数据库加载 SOAP 配置失败，使用 .env 默认值: {e}")
