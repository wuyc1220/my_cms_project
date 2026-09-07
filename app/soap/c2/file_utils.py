"""
C2 文件存储工具模块

提供统一的 C2 XML 文件路径生成和上传功能。
文件存储路径格式: c2/YYYYMM/{filename}
"""
from datetime import datetime
from typing import TYPE_CHECKING
import uuid

from loguru import logger

if TYPE_CHECKING:
    from app.internal.cms_biz_orchestration.services.storage import StorageService


def generate_c2_filename(
    prefix: str,
    entity_type: str,
    entity_id: int,
    timestamp: datetime | None = None,
) -> str:
    """
    生成 C2 XML 文件路径

    路径格式: YYYYMM/{prefix}_{entity_type}_{entity_id}_{uuid8}_{timestamp}.xml

    Args:
        prefix: 文件名前缀（如 'pictures', 'adi', 'category_sync'）
        entity_type: 实体类型（如 'schedule', 'channel', 'program'）
        entity_id: 实体ID
        timestamp: 时间戳，默认为当前时间

    Returns:
        完整的文件路径（相对于 c2 目录）
    """
    if timestamp is None:
        timestamp = datetime.now()

    date_folder = timestamp.strftime("%Y%m")  # 年月作为文件夹名
    ts = timestamp.strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]

    filename = f"{date_folder}/{prefix}_{entity_type}_{entity_id}_{short_uuid}_{ts}.xml"
    return filename


def upload_c2_xml(
    xml_content: str,
    filename: str,
    storage_service: "StorageService",
) -> str:
    """
    上传 C2 XML 文件到存储

    Args:
        xml_content: XML 内容字符串
        filename: 文件名（包含路径，如 '202601/pictures_schedule_178_xxx.xml'）
        storage_service: 存储服务实例

    Returns:
        存储的完整文件路径
    """
    result = storage_service.save_file(
        file_content=xml_content.encode("utf-8"),
        filename=filename,
        category="c2",
        skip_uuid_prefix=True,
    )
    logger.info(f"[C2] XML 已上传: {result['file_path']}")
    return result["file_path"]


def generate_and_upload_c2_xml(
    xml_content: str,
    prefix: str,
    entity_type: str,
    entity_id: int,
    storage_service: "StorageService",
    timestamp: datetime | None = None,
) -> str:
    """
    生成文件名并上传 C2 XML 文件（一站式函数）

    Args:
        xml_content: XML 内容字符串
        prefix: 文件名前缀
        entity_type: 实体类型
        entity_id: 实体ID
        storage_service: 存储服务实例
        timestamp: 时间戳，默认为当前时间

    Returns:
        存储的完整文件路径
    """
    filename = generate_c2_filename(
        prefix=prefix,
        entity_type=entity_type,
        entity_id=entity_id,
        timestamp=timestamp,
    )
    return upload_c2_xml(xml_content, filename, storage_service)
