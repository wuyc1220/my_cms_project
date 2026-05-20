"""SOAP 内容分发配置管理"""
from pydantic_settings import BaseSettings
from typing import Optional


class SOAPSettings(BaseSettings):
    """SOAP 内容分发配置"""
    
    # 是否启用 SOAP 内容分发
    enabled: bool = False
    
    # CSP 配置（本系统标识）
    csp_id: str = "SAAT-CMS-001"
    
    # LSP 配置（对接方标识）
    lsp_id: str = "ZTE-MW-001"
    
    # LSP SOAP 服务端地址
    lsp_soap_url: str = "http://lsp-server.com/soap/wsdl"
    
    # XML 指令文件存放路径（本地）
    xml_storage_path: str = "./soap_commands"
    
    # XML 指令文件访问 URL（LSP 可访问的地址）
    xml_base_url: str = "http://cms-server.com/commands"

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
    
    class Config:
        env_file = ".env"
        env_prefix = "SOAP_"
        extra = "ignore"  # 忽略 .env 中非 SOAP_ 前缀的变量，避免与全局 settings 冲突


# 全局 SOAP 配置实例
soap_settings = SOAPSettings()
