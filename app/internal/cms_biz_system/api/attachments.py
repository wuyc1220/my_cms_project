"""
通用附件上传 API

提供不绑定业务实体的纯文件上传/下载能力，供各业务模块复用。
文件实际存储由 StorageService（SFTP / 本地）接管。

接口列表：
    POST /attachments/upload   上传文件（multipart/form-data）
    GET  /attachments/download 文件下载（query: path, inline）
"""

import asyncio
import mimetypes
import os
import re
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from loguru import logger
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, get_db
from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException
from app.common.core.i18n import get_msg
from app.config import settings
from app.internal.cms_biz_orchestration.services.storage import LOCAL_UPLOAD_DIR, storage_service
from app.internal.cms_biz_system.models.user import User

router = APIRouter()

# ── 安全校验 ──────────────────────────────────────────────

# category 只允许字母、数字、下划线、连字符、斜杠
_CATEGORY_PATTERN = re.compile(r"^[a-zA-Z0-9_\-/]+$")
# 文件名禁止路径穿越字符
_FILENAME_PATTERN = re.compile(r"[\\<>\"|?*\x00-\x1f]")


def _sanitize_category(category: str | None) -> str:
    """清理并校验 category，防止路径穿越。"""
    cat = (category or "general").strip().lower()
    # 去除首尾斜杠，防止绝对路径或空目录
    cat = cat.strip("/")
    if not cat:
        cat = "general"
    if not _CATEGORY_PATTERN.match(cat):
        raise BusinessException(ErrorCode.INVALID_FILE_FORMAT, get_msg("INVALID_FILE_FORMAT"))
    # 禁止 .. 路径穿越
    if ".." in cat.split("/"):
        raise BusinessException(ErrorCode.INVALID_FILE_FORMAT, get_msg("INVALID_FILE_FORMAT"))
    return cat


def _sanitize_filename(filename: str | None) -> str:
    """清理文件名，移除非法字符。"""
    name = (filename or "unnamed").strip()
    # 移除路径穿越字符
    name = os.path.basename(name.replace("\\", "/"))
    name = _FILENAME_PATTERN.sub("", name)
    if not name:
        name = "unnamed"
    return name


# ── Pydantic 模型 ─────────────────────────────────────────

class AttachmentUploadResult(BaseModel):
    file_path: str
    file_name: str
    file_size: int
    file_url: str


# ── 接口 ──────────────────────────────────────────────────

@router.post("/attachments/upload", response_model=AttachmentUploadResult)
async def upload_attachment(
    file: UploadFile = File(...),
    category: str = Query(default="general", description="文件分类目录，如 contracts、pictures、general"),
    _: User = Depends(get_current_user),
):
    """
    通用文件上传接口。

    - 文件大小受 `max_file_size` 限制（默认 2GB，可通过配置调整）。
    - category 用于组织存储目录，仅允许字母、数字、下划线、连字符、斜杠。
    - 返回文件元数据及可直接访问的 URL。
    """
    # 获取文件信息
    cat = _sanitize_category(category)
    filename = _sanitize_filename(file.filename)
    file_size = file.size or 0
    
    # 文件大小校验
    if file_size > settings.max_file_size:
        raise BusinessException(ErrorCode.FILE_TOO_LARGE, get_msg("FILE_TOO_LARGE"))

    # 对于大文件，使用流式上传避免内存问题
    # 读取文件内容（FastAPI 的 UploadFile 已经使用 SpooledTemporaryFile 优化）
    logger.info(f"开始读取文件: {filename}, 大小: {file_size} bytes")
    content = await file.read()
    logger.info(f"文件读取完成: {filename}, 读取了 {len(content)} bytes")
    
    logger.info(f"开始上传文件到存储: {filename}")
    # 使用 run_in_executor 将同步的 SFTP 上传放到线程池执行，避免阻塞事件循环
    loop = asyncio.get_event_loop()
    try:
        saved = await asyncio.wait_for(
            loop.run_in_executor(None, storage_service.save_file, content, filename, cat),
            timeout=240,  # 整体上传超时240秒（4分钟），前端超时300秒
        )
    except asyncio.TimeoutError:
        logger.error(f"文件上传超时: {filename}, 大小: {file_size} bytes")
        raise BusinessException(ErrorCode.INTERNAL_ERROR, get_msg("UPLOAD_TIMEOUT"))
    logger.info(f"文件上传完成: {saved['file_path']}")

    return AttachmentUploadResult(
        file_path=saved["file_path"],
        file_name=saved["file_name"],
        file_size=saved["file_size"],
        file_url=storage_service.get_file_url(saved["file_path"]),
    )


@router.get("/attachments/download")
async def download_attachment(
    path: str = Query(..., description="文件存储相对路径"),
    inline: bool = Query(False, description="是否内联显示（true 时返回图片等媒体可直接展示，适用 <img> 标签）"),
):
    """
    通用文件下载/预览接口（自动适配存储后端：SFTP / FTP / Local）。

    - path 为 upload 接口返回的 `file_path`。
    - inline=false（默认）：以附件形式下载文件。
    - inline=true：以内联形式返回文件内容，浏览器可直接展示图片等媒体。
    - 不需要认证，方便在 <img> 等标签中直接引用。
    """
    decoded_path = unquote(path)
    filename = decoded_path.rsplit("/", 1)[-1]

    # 获取正确的 MIME 类型
    media_type, _ = mimetypes.guess_type(filename)
    if not media_type:
        media_type = "application/octet-stream"

    # 使用 storage_service 统一读取（自动适配后端）
    try:
        file_content = storage_service.get_file(decoded_path)
    except FileNotFoundError:
        raise NotFoundException(ErrorCode.NOT_FOUND, get_msg("FILE_NOT_FOUND"))
    except Exception as e:
        raise BusinessException(ErrorCode.INTERNAL_ERROR, f"Failed to read file: {e}")

    # 设置 Content-Disposition
    encoded_name = quote(filename, safe="")
    try:
        ascii_name = filename.encode("ascii").decode("ascii")
        disposition_type = "inline" if inline else "attachment"
        disposition = f'{disposition_type}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    except (UnicodeEncodeError, UnicodeDecodeError):
        disposition_type = "inline" if inline else "attachment"
        disposition = f"{disposition_type}; filename*=UTF-8''{encoded_name}"

    return Response(
        content=file_content,
        media_type=media_type,
        headers={"Content-Disposition": disposition},
    )
