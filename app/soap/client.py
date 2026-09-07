"""SOAP 客户端实现 - 直接发送原始 XML"""
import uuid
# defusedxml：解析 LSP 返回的不可信 XML 时禁用实体/DTD，防 XML 炸弹（billion laughs）DoS
from defusedxml import ElementTree as ET
from typing import Dict, Any, Optional
from loguru import logger
import requests

from .config import soap_settings
from app.common.core.i18n import get_msg


# SOAP 请求 XML 模板（参照 LSP 提供的示例格式）
EXEC_CMD_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding/"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns:m0="http://schemas.xmlsoap.org/soap/encoding/">
<SOAP-ENV:Body>
<m:ExecCmd xmlns:m="iptv" SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
    <CSPID xsi:type="m0:string">{csp_id}</CSPID>
    <LSPID xsi:type="m0:string">{lsp_id}</LSPID>
    <CorrelateID xsi:type="m0:string">{correlate_id}</CorrelateID>
    <CmdFileURL xsi:type="m0:string">{cmd_file_url}</CmdFileURL>
</m:ExecCmd>
</SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


class SOAPClient:
    """SOAP 内容分发客户端（原始 XML 模式）"""
    
    def __init__(self):
        """初始化 SOAP 客户端"""
        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": ""
        })
        if soap_settings.enabled:
            logger.info(f"SOAP 客户端初始化成功，LSP 地址: {soap_settings.lsp_soap_url}")
    
    def _parse_response(self, response_text: str) -> Dict[str, Any]:
        """
        解析 SOAP 响应 XML，提取 Result 和 ErrorDescription
        
        响应格式示例:
        <ExecCmdReturn>
            <Result>0</Result>
            <ErrorDescription></ErrorDescription>
        </ExecCmdReturn>
        """
        try:
            # 去除命名空间前缀以简化解析
            root = ET.fromstring(response_text)
            
            # 递归查找 Result 和 ErrorDescription 节点
            result_val = None
            error_desc = ""
            
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "Result":
                    result_val = elem.text
                elif tag == "ErrorDescription":
                    error_desc = elem.text or ""
            
            if result_val is not None:
                return {
                    "Result": int(result_val),
                    "ErrorDescription": error_desc
                }
            else:
                return {
                    "Result": -1,
                    "ErrorDescription": f"无法从响应中解析 Result: {response_text[:500]}"
                }
        except Exception as e:
            return {
                "Result": -1,
                "ErrorDescription": f"响应解析失败: {e}, 原始响应: {response_text[:500]}"
            }
    
    def send_exec_cmd_req(
        self,
        cmd_file_url: str,
        correlate_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        发送执行指令请求 (ExecCmd)
        
        Args:
            cmd_file_url: XML 指令文件 URL
            correlate_id: 关联 ID（可选，不传则自动生成）
            
        Returns:
            响应结果 {"success": bool, "correlate_id": str, "result": int, "error_description": str}
        """
        if not soap_settings.enabled:
            raise RuntimeError(get_msg("SOAP_CLIENT_NOT_ENABLED"))
        
        if not correlate_id:
            correlate_id = uuid.uuid4().hex  # 32位无横线 UUID
        
        # 构造 SOAP XML 请求体
        soap_body = EXEC_CMD_TEMPLATE.format(
            csp_id=soap_settings.csp_id,
            lsp_id=soap_settings.lsp_id,
            correlate_id=correlate_id,
            cmd_file_url=cmd_file_url
        )
        
        try:
            logger.info(
                f"发送 ExecCmd - CorrelateID: {correlate_id}, "
                f"CmdFileURL: {cmd_file_url}, "
                f"目标地址: {soap_settings.lsp_soap_url}"
            )
            logger.info(f"========== SOAP 请求 XML ==========\n{soap_body}\n====================================")
            
            # 发送 HTTP POST 请求
            response = self.session.post(
                soap_settings.lsp_soap_url,
                data=soap_body.encode("utf-8"),
                timeout=soap_settings.timeout
            )
            
            logger.info(f"SOAP 响应状态码: {response.status_code}")
            logger.info(f"========== SOAP 响应 XML ==========\n{response.text}\n====================================")
            
            # 解析响应
            result = self._parse_response(response.text)
            
            logger.info(
                f"ExecCmd 响应 - CorrelateID: {correlate_id}, "
                f"Result: {result['Result']}, "
                f"ErrorDescription: {result['ErrorDescription']}"
            )
            
            return {
                "success": result["Result"] == 0,
                "correlate_id": correlate_id,
                "result": result["Result"],
                "error_description": result["ErrorDescription"]
            }
            
        except requests.exceptions.Timeout:
            logger.error(f"ExecCmd 请求超时 - CorrelateID: {correlate_id}")
            return {
                "success": False,
                "correlate_id": correlate_id,
                "result": -1,
                "error_description": f"请求超时（{soap_settings.timeout}秒）"
            }
        except Exception as e:
            logger.error(f"ExecCmd 调用失败 - CorrelateID: {correlate_id}, 错误: {e}")
            return {
                "success": False,
                "correlate_id": correlate_id,
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
            correlate_id = uuid.uuid4().hex
            
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
            correlate_id = uuid.uuid4().hex
            
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
