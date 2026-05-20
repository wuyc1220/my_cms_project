"""SOAP 客户端实现"""
import uuid
from typing import Dict, Any, Optional
from loguru import logger

try:
    from zeep import Client
    from zeep.transports import Transport
    import requests
    ZEEP_AVAILABLE = True
except ImportError:
    ZEEP_AVAILABLE = False
    logger.warning("zeep 库未安装，SOAP 功能不可用。请运行: pip install zeep")

from .config import soap_settings


class SOAPClient:
    """SOAP 内容分发客户端"""
    
    def __init__(self):
        """初始化 SOAP 客户端"""
        if not ZEEP_AVAILABLE:
            raise ImportError("zeep 库未安装，请运行: pip install zeep")
        
        self.client = None
        if soap_settings.enabled:
            try:
                transport = Transport(timeout=soap_settings.timeout)
                self.client = Client(soap_settings.lsp_soap_url, transport=transport)
                logger.info(f"SOAP 客户端初始化成功，LSP 地址: {soap_settings.lsp_soap_url}")
            except Exception as e:
                logger.error(f"SOAP 客户端初始化失败: {e}")
                raise
    
    def send_exec_cmd_req(
        self,
        cmd_file_url: str,
        correlate_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        发送执行指令请求 (ExecCmdReq)
        
        Args:
            cmd_file_url: XML 指令文件 URL
            correlate_id: 关联 ID（可选，不传则自动生成）
            
        Returns:
            响应结果 {"Result": 0, "ErrorDescription": ""}
        """
        if not self.client:
            raise RuntimeError("SOAP 客户端未初始化，请检查 SOAP_ENABLED 配置")
        
        if not correlate_id:
            correlate_id = str(uuid.uuid4())
        
        try:
            logger.info(
                f"发送 ExecCmdReq - CorrelateID: {correlate_id}, "
                f"CmdFileURL: {cmd_file_url}"
            )
            
            # 调用 LSP 的 ExecCmdReq 接口
            result = self.client.service.ExecCmdReq(
                CSPID=soap_settings.csp_id,
                LSPID=soap_settings.lsp_id,
                CorrelateID=correlate_id,
                CmdFileURL=cmd_file_url
            )
            
            logger.info(
                f"ExecCmdReq 响应 - CorrelateID: {correlate_id}, "
                f"Result: {result.get('Result')}"
            )
            
            return {
                "success": result.get("Result") == 0,
                "correlate_id": correlate_id,
                "result": result.get("Result"),
                "error_description": result.get("ErrorDescription", "")
            }
            
        except Exception as e:
            logger.error(f"ExecCmdReq 调用失败 - CorrelateID: {correlate_id}, 错误: {e}")
            return {
                "success": False,
                "correlate_id": correlate_id,
                "result": -1,
                "error_description": str(e)
            }
    
    def send_result_notify_res(
        self,
        correlate_id: str,
        result: int = 0,
        error_description: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        发送结果通知响应 (ResultNotifyRes)
        
        Args:
            correlate_id: 关联 ID
            result: 接收结果 (0=成功, -1=失败)
            error_description: 错误描述
            
        Returns:
            响应结果
        """
        if not self.client:
            raise RuntimeError("SOAP 客户端未初始化")
        
        try:
            logger.info(
                f"发送 ResultNotifyRes - CorrelateID: {correlate_id}, "
                f"Result: {result}"
            )
            
            response = self.client.service.ResultNotifyRes(
                Result=result,
                ErrorDescription=error_description or ""
            )
            
            logger.info(f"ResultNotifyRes 响应 - CorrelateID: {correlate_id}")
            
            return {
                "success": response.get("Result") == 0,
                "result": response.get("Result"),
                "error_description": response.get("ErrorDescription", "")
            }
            
        except Exception as e:
            logger.error(f"ResultNotifyRes 调用失败 - CorrelateID: {correlate_id}, 错误: {e}")
            return {
                "success": False,
                "result": -1,
                "error_description": str(e)
            }


class SOAPCommandService:
    """SOAP 指令服务（高级封装）"""
    
    def __init__(self):
        """初始化服务"""
        from .xml_generator import XMLCommandGenerator
        self.soap_client = SOAPClient()
        self.xml_generator = XMLCommandGenerator()
    
    def publish_content(
        self,
        content_id: str,
        content_type: str,
        title: str,
        file_url: str,
        duration: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        发布内容到 LSP
        
        Args:
            content_id: 内容 ID
            content_type: 内容类型
            title: 内容标题
            file_url: 内容文件 URL
            duration: 时长（秒）
            metadata: 元数据
            
        Returns:
            发布结果
        """
        try:
            # 1. 生成 XML 指令文件
            xml_filepath = self.xml_generator.generate_content_publish_xml(
                content_id=content_id,
                content_type=content_type,
                title=title,
                file_url=file_url,
                duration=duration,
                metadata=metadata
            )
            
            # 2. 获取 XML 文件 URL
            xml_url = self.xml_generator.get_xml_url(xml_filepath)
            
            # 3. 生成关联 ID
            correlate_id = str(uuid.uuid4())
            
            # 4. 发送 SOAP 请求
            result = self.soap_client.send_exec_cmd_req(
                cmd_file_url=xml_url,
                correlate_id=correlate_id
            )
            
            logger.info(
                f"内容发布请求完成 - ContentID: {content_id}, "
                f"CorrelateID: {correlate_id}, Success: {result['success']}"
            )
            
            return {
                "success": result["success"],
                "content_id": content_id,
                "correlate_id": correlate_id,
                "xml_url": xml_url,
                "result": result["result"],
                "error_description": result["error_description"]
            }
            
        except Exception as e:
            logger.error(f"内容发布失败 - ContentID: {content_id}, 错误: {e}")
            return {
                "success": False,
                "content_id": content_id,
                "correlate_id": None,
                "xml_url": None,
                "result": -1,
                "error_description": str(e)
            }
    
    def unpublish_content(
        self,
        content_id: str,
        content_type: str,
        reason: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        从 LSP 下线内容
        
        Args:
            content_id: 内容 ID
            content_type: 内容类型
            reason: 下线原因
            
        Returns:
            下线结果
        """
        try:
            # 1. 生成 XML 指令文件
            xml_filepath = self.xml_generator.generate_content_unpublish_xml(
                content_id=content_id,
                content_type=content_type,
                reason=reason
            )
            
            # 2. 获取 XML 文件 URL
            xml_url = self.xml_generator.get_xml_url(xml_filepath)
            
            # 3. 生成关联 ID
            correlate_id = str(uuid.uuid4())
            
            # 4. 发送 SOAP 请求
            result = self.soap_client.send_exec_cmd_req(
                cmd_file_url=xml_url,
                correlate_id=correlate_id
            )
            
            logger.info(
                f"内容下线请求完成 - ContentID: {content_id}, "
                f"CorrelateID: {correlate_id}, Success: {result['success']}"
            )
            
            return {
                "success": result["success"],
                "content_id": content_id,
                "correlate_id": correlate_id,
                "xml_url": xml_url,
                "result": result["result"],
                "error_description": result["error_description"]
            }
            
        except Exception as e:
            logger.error(f"内容下线失败 - ContentID: {content_id}, 错误: {e}")
            return {
                "success": False,
                "content_id": content_id,
                "correlate_id": None,
                "xml_url": None,
                "result": -1,
                "error_description": str(e)
            }
