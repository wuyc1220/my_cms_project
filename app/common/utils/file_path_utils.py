"""
通用文件路径工具模块

提供统一的文件路径生成和组织功能，支持按时间分文件夹存储。
"""
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


def generate_timestamped_path(
    base_category: str,
    *sub_paths: str,
    filename: str | None = None,
    timestamp: datetime | None = None,
) -> str:
    """
    生成带时间戳的文件路径

    路径格式: {base_category}/YYYYMM/{sub_paths}/{filename}
    或: {base_category}/YYYYMM/{sub_paths}/  (当 filename 为 None 时)

    Args:
        base_category: 基础分类目录（如 'pictures', 'contracts', 'materials'）
        *sub_paths: 子路径组件（如 entity_type, entity_id）
        filename: 文件名（可选）
        timestamp: 时间戳（默认为当前时间）

    Returns:
        完整的相对路径

    Examples:
        >>> generate_timestamped_path("pictures", "schedule", "178", filename="poster.jpg")
        'pictures/202601/schedule/178/poster.jpg'

        >>> generate_timestamped_path("contracts", "2024", filename="contract.pdf")
        'contracts/202601/2024/contract.pdf'
    """
    if timestamp is None:
        timestamp = datetime.now()

    date_folder = timestamp.strftime("%Y%m")

    # 构建路径组件
    path_parts = [base_category, date_folder]
    path_parts.extend(sub_paths)

    if filename:
        path_parts.append(filename)

    return "/".join(path_parts)


def generate_c2_style_path(
    prefix: str,
    entity_type: str,
    entity_id: int,
    timestamp: datetime | None = None,
) -> str:
    """
    生成 C2 风格的文件路径（用于 XML 文件）

    路径格式: c2/YYYYMM/{prefix}_{entity_type}_{entity_id}_{timestamp}.xml

    Args:
        prefix: 前缀（如 'pictures', 'adi', 'category_sync'）
        entity_type: 实体类型
        entity_id: 实体ID
        timestamp: 时间戳（默认为当前时间）

    Returns:
        完整的相对路径
    """
    from datetime import datetime as dt
    import uuid

    if timestamp is None:
        timestamp = dt.now()

    date_folder = timestamp.strftime("%Y%m")
    ts = timestamp.strftime("%Y%m%d_%H%M%S")
    short_uuid = uuid.uuid4().hex[:8]

    filename = f"{prefix}_{entity_type}_{entity_id}_{short_uuid}_{ts}.xml"

    return f"c2/{date_folder}/{filename}"
