"""XML 文件下载服务

LSP 通过此接口下载 XML 指令文件，文件存储在 SFTP 服务器上。
"""
from fastapi import APIRouter, Response
from loguru import logger

from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.common.core.i18n import get_msg
from app.common.core.exceptions import BusinessException, ErrorCode

router = APIRouter(prefix="/commands", tags=["SOAP指令文件"])


@router.get("/{filename}")
async def serve_xml_file(filename: str):
    """
    提供 XML 指令文件下载服务（从 SFTP 读取）

    LSP 通过此接口下载 XML 指令文件
    """
    import asyncio

    if not filename.endswith('.xml'):
        raise BusinessException(ErrorCode.XML_FILE_ONLY)

    try:
        # 从 SFTP 读取文件内容（放到线程池，避免同步 I/O 阻塞事件循环）
        file_content = await asyncio.to_thread(
            storage_service.get_file, f"c2/{filename}"
        )

        logger.info(f"从 SFTP 读取 XML 文件: c2/{filename}, 大小: {len(file_content)} bytes")

        return Response(
            content=file_content,
            media_type="application/xml",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        logger.error(f"从 SFTP 读取文件失败 - filename: {filename}, 错误: {e}")
        raise BusinessException(ErrorCode.FILE_NOT_FOUND)