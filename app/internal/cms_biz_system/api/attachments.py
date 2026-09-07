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
from app.common.crypto import decrypt_storage_url
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
    storage_url: str


# ── 接口 ──────────────────────────────────────────────────

@router.post("/attachments/upload", response_model=AttachmentUploadResult)
async def upload_attachment(
    file: UploadFile = File(...),
    category: str = Query(default="general", description="文件分类目录，如 contracts、pictures、general"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    通用文件上传接口。

    - 文件大小受数据库参数 `MAX_FILE_SIZE` 限制（单位：字节，读取失败时回退到 .env 的 max_file_size）。
    - category 用于组织存储目录，仅允许字母、数字、下划线、连字符、斜杠。
    - 返回文件元数据及可直接访问的 URL。
    """
    # 获取文件信息
    cat = _sanitize_category(category)
    filename = _sanitize_filename(file.filename)
    file_size = file.size or 0
    
    # 文件大小校验（优先从数据库读取配置，单位：字节）
    from app.internal.cms_biz_system.services.config_service import get_config_int

    max_file_size = settings.max_file_size  # .env 默认值（字节）
    try:
        db_max_size = await get_config_int(db, "MAX_FILE_SIZE", default_value=max_file_size)
        if db_max_size != max_file_size:
            logger.debug(f"MAX_FILE_SIZE 从数据库加载: {db_max_size} 字节 (覆盖.env: {max_file_size} 字节)")
            max_file_size = db_max_size
    except Exception as e:
        logger.warning(f"从数据库加载 MAX_FILE_SIZE 失败，使用 .env 默认值: {e}")
    
    if file_size > max_file_size:
        raise BusinessException(ErrorCode.FILE_TOO_LARGE, get_msg("FILE_TOO_LARGE"))

    # 对于大文件，使用流式上传避免内存问题
    # 读取文件内容（FastAPI 的 UploadFile 已经使用 SpooledTemporaryFile 优化）
    logger.info(f"开始读取文件: {filename}, 大小: {file_size} bytes")
    content = await file.read()
    logger.info(f"文件读取完成: {filename}, 读取了 {len(content)} bytes")
    
    # 生成带时间戳的存储目录: {category}/YYYYMM
    # 文件名传纯文件名（save_file 内部追加 uuid 前缀防重名），避免返回的 file_name 带目录前缀
    from app.common.utils.file_path_utils import generate_timestamped_path
    date_dir = generate_timestamped_path(cat)

    logger.info(f"开始上传文件到存储: {date_dir}/{filename}")
    # 使用 run_in_executor 将同步的 SFTP 上传放到线程池执行，避免阻塞事件循环
    loop = asyncio.get_event_loop()
    try:
        saved = await asyncio.wait_for(
            loop.run_in_executor(None, storage_service.save_file, content, filename, date_dir),
            timeout=240,  # 整体上传超时240秒（4分钟），前端超时300秒
        )
    except asyncio.TimeoutError:
        logger.error(f"文件上传超时: {date_dir}/{filename}, 大小: {file_size} bytes")
        raise BusinessException(ErrorCode.INTERNAL_ERROR, get_msg("UPLOAD_TIMEOUT"))
    logger.info(f"文件上传完成: {saved['file_path']}")

    return AttachmentUploadResult(
        file_path=saved["file_path"],
        file_name=saved["file_name"],
        file_size=saved["file_size"],
        file_url=storage_service.get_file_url(saved["file_path"]),
        storage_url=storage_service.get_storage_url(saved["file_path"]),
    )


@router.get("/attachments/download")
async def download_attachment(
    path: str = Query(..., description="文件存储路径(相对路径或加密URL)"),
    inline: bool = Query(False, description="是否内联显示(true 时返回图片等媒体可直接展示,适用 <img> 标签)"),
):
    """
    通用文件下载/预览接口(自动适配存储后端:SFTP / FTP / Local)。

    - path 支持两种格式:
      1. 相对路径: pictures/xxx.jpg (旧数据)
      2. 加密URL: 经过加密的完整FTP/SFTP URL (新数据)
    - inline=false(默认):以附件形式下载文件。
    - inline=true:以内联形式返回文件内容,浏览器可直接展示图片等媒体。
    - 不需要认证,方便在 <img> 等标签中直接引用。
    """
    # 处理URL编码:axios的params会自动编码,需要解码
    decoded_path = unquote(path)

    # 判断是否为外部FTP/SFTP的加密URL（解密后是完整URL）
    resolved = decrypt_storage_url(decoded_path)
    if resolved.startswith(("sftp://", "ftp://")):
        # 外部FTP场景：直接使用解密后的完整URL下载
        file_path = decoded_path
        filename = resolved.rsplit("/", 1)[-1]
    else:
        # 本地上传场景：转换为相对路径
        file_path = storage_service._to_relative_path(decoded_path)
        filename = file_path.rsplit("/", 1)[-1]

    logger.debug(f"下载文件 - 原始path: {path[:100]}..., 解密后: {resolved[:100]}..., 使用路径: {file_path}")

    # 获取正确的 MIME 类型
    media_type, _ = mimetypes.guess_type(filename)
    if not media_type:
        media_type = "application/octet-stream"

    # 使用 storage_service 统一读取(自动适配后端)
    # 注意：get_file 为同步阻塞 I/O（SFTP 每次新建连接，单次可达数百 ms），
    # 必须放入线程池执行，否则会卡住整个事件循环，拖慢所有并发接口
    try:
        file_content = await asyncio.to_thread(storage_service.get_file, file_path)
    except FileNotFoundError:
        raise NotFoundException(ErrorCode.NOT_FOUND, get_msg("FILE_NOT_FOUND"))
    except Exception as e:
        logger.error(f"读取文件失败: {e}, path: {file_path}")
        raise BusinessException(ErrorCode.INTERNAL_ERROR, get_msg("FILE_READ_ERROR", error=e))

    # 设置 Content-Disposition
    encoded_name = quote(filename, safe="")
    try:
        ascii_name = filename.encode("ascii").decode("ascii")
        disposition_type = "inline" if inline else "attachment"
        disposition = f'{disposition_type}; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    except (UnicodeEncodeError, UnicodeDecodeError):
        disposition_type = "inline" if inline else "attachment"
        disposition = f"{disposition_type}; filename*=UTF-8''{encoded_name}"

    # 内联展示的图片等静态资源允许浏览器缓存（海报文件名带 uuid 前缀，
    # 重新上传会生成新 URL，不存在缓存后内容过期的问题），
    # 避免每次进页面都重新走 SFTP 下载
    resp_headers = {"Content-Disposition": disposition}
    if inline:
        resp_headers["Cache-Control"] = "public, max-age=86400"

    return Response(
        content=file_content,
        media_type=media_type,
        headers=resp_headers,
    )
