"""XML 指令生成器"""
import os
import uuid
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path
from loguru import logger

from .config import soap_settings


class XMLCommandGenerator:
    """XML 指令文件生成器"""
    
    def __init__(self):
        """初始化 XML 生成器"""
        # 确保存储目录存在
        self.storage_path = Path(soap_settings.xml_storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)
        logger.info(f"XML 指令存储路径: {self.storage_path.absolute()}")
    
    def generate_content_publish_xml(
        self,
        content_id: str,
        content_type: str,
        title: str,
        file_url: str,
        duration: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        生成内容发布指令 XML
        
        Args:
            content_id: 内容 ID
            content_type: 内容类型 (movie/series/channel)
            title: 内容标题
            file_url: 内容文件 URL
            duration: 时长（秒）
            metadata: 额外元数据
            
        Returns:
            XML 文件路径
        """
        cmd_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"publish_{content_id}_{timestamp}.xml"
        filepath = self.storage_path / filename
        
        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Command>
    <Header>
        <CommandID>{cmd_id}</CommandID>
        <CommandType>ContentPublish</CommandType>
        <Timestamp>{datetime.now().isoformat()}</Timestamp>
    </Header>
    <Content>
        <ID>{content_id}</ID>
        <Type>{content_type}</Type>
        <Title>{title}</Title>
        <FileURL>{file_url}</FileURL>
        {f'<Duration>{duration}</Duration>' if duration else ''}
    </Content>
    {self._build_metadata_xml(metadata) if metadata else ''}
</Command>"""
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        
        logger.info(f"生成内容发布 XML: {filepath}")
        return str(filepath)
    
    def generate_content_unpublish_xml(
        self,
        content_id: str,
        content_type: str,
        reason: Optional[str] = None
    ) -> str:
        """
        生成内容下线指令 XML
        
        Args:
            content_id: 内容 ID
            content_type: 内容类型
            reason: 下线原因
            
        Returns:
            XML 文件路径
        """
        cmd_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"unpublish_{content_id}_{timestamp}.xml"
        filepath = self.storage_path / filename
        
        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Command>
    <Header>
        <CommandID>{cmd_id}</CommandID>
        <CommandType>ContentUnpublish</CommandType>
        <Timestamp>{datetime.now().isoformat()}</Timestamp>
    </Header>
    <Content>
        <ID>{content_id}</ID>
        <Type>{content_type}</Type>
        {f'<Reason>{reason}</Reason>' if reason else ''}
    </Content>
</Command>"""
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        
        logger.info(f"生成内容下线 XML: {filepath}")
        return str(filepath)
    
    def generate_query_status_xml(
        self,
        content_id: str,
        content_type: str
    ) -> str:
        """
        生成状态查询指令 XML
        
        Args:
            content_id: 内容 ID
            content_type: 内容类型
            
        Returns:
            XML 文件路径
        """
        cmd_id = str(uuid.uuid4())
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"query_{content_id}_{timestamp}.xml"
        filepath = self.storage_path / filename
        
        xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<Command>
    <Header>
        <CommandID>{cmd_id}</CommandID>
        <CommandType>QueryStatus</CommandType>
        <Timestamp>{datetime.now().isoformat()}</Timestamp>
    </Header>
    <Content>
        <ID>{content_id}</ID>
        <Type>{content_type}</Type>
    </Content>
</Command>"""
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(xml_content)
        
        logger.info(f"生成状态查询 XML: {filepath}")
        return str(filepath)
    
    def get_xml_url(self, filepath: str) -> str:
        """
        获取 XML 文件的访问 URL
        
        Args:
            filepath: XML 文件本地路径
            
        Returns:
            可访问的 URL
        """
        filename = Path(filepath).name
        return f"{soap_settings.xml_base_url.rstrip('/')}/{filename}"
    
    def _build_metadata_xml(self, metadata: Dict[str, Any]) -> str:
        """构建元数据 XML 片段"""
        if not metadata:
            return ""
        
        xml_parts = ["    <Metadata>"]
        for key, value in metadata.items():
            xml_parts.append(f"        <{key}>{value}</{key}>")
        xml_parts.append("    </Metadata>")
        
        return "\n".join(xml_parts)
