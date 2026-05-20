from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from app.common.core.exceptions import NotFoundException, ErrorCode, BusinessException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user
from app.common.dependencies import get_db
from app.common.core.i18n import get_msg
from app.internal.cms_biz_metada.models.basic import Picture, PosterSize
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.internal.cms_biz_orchestration.repositories import get_content_by_id
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
)

# 内容类实体的 entity_type（用于判断是否触发流程节点）
CONTENT_ENTITY_TYPES = {"program", "series", "channel", "schedule"}

router = APIRouter()


def _content_id_for_picture(entity_type: str, entity_id: int) -> int | None:
    """仅当图片属于 Content 实体时，才关联 Activity Log。"""
    return entity_id if entity_type in CONTENT_ENTITY_TYPES else None


class PictureItem(BaseModel):
    id: int
    entity_type: str
    entity_id: int
    poster_size_id: int
    file_name: str
    file_path: str
    file_size: int
    width: int | None
    height: int | None
    created_at: datetime
    url: str


class PictureCreate(BaseModel):
    """通过通用附件上传后创建 Picture 记录的入参"""
    entity_type: str
    entity_id: int
    poster_size_id: int
    file_path: str
    file_name: str
    file_size: int


def _to_item(pic: Picture) -> PictureItem:
    return PictureItem(
        id=pic.id,
        entity_type=pic.entity_type,
        entity_id=pic.entity_id,
        poster_size_id=pic.poster_size_id,
        file_name=pic.file_name,
        file_path=pic.file_path,
        file_size=pic.file_size,
        width=pic.width,
        height=pic.height,
        created_at=pic.created_at,
        url=storage_service.get_file_url(pic.file_path),
    )


@router.get("/pictures", response_model=list[PictureItem])
async def list_pictures(
    entity_type: str = Query(...),
    entity_id: int = Query(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    rows = (
        await db.execute(
            select(Picture)
            .where(
                Picture.entity_type == entity_type,
                Picture.entity_id == entity_id,
                Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
            )
            .order_by(Picture.created_at)
        )
    ).scalars().all()
    return [_to_item(r) for r in rows]


@router.post("/pictures", response_model=PictureItem)
async def create_picture(
    data: PictureCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    创建图片记录（文件已通过通用附件 API 上传）。

    校验 PosterSize 规格（扩展名、文件大小、图片尺寸），提取图片宽高，写入 picture 表。
    """
    ps = (await db.execute(select(PosterSize).where(PosterSize.id == data.poster_size_id, PosterSize.is_deleted.is_(False)))).scalar_one_or_none()
    if not ps:
        raise BusinessException(ErrorCode.POSTER_SIZE_NOT_FOUND, get_msg("POSTER_SIZE_NOT_FOUND"))

    # 校验扩展名
    ext = data.file_name.rsplit(".", 1)[-1].lower() if "." in data.file_name else ""
    allowed_exts = [e.extension.lower() for e in ps.extensions]
    if allowed_exts and ext not in allowed_exts:
        raise BusinessException(ErrorCode.INVALID_FILE_FORMAT, get_msg("INVALID_FILE_FORMAT"))

    # 校验文件大小
    if ps.max_file_size_kb > 0 and data.file_size > ps.max_file_size_kb * 1024:
        raise BusinessException(ErrorCode.FILE_TOO_LARGE, get_msg("FILE_TOO_LARGE"))

    # 尝试从存储读取文件以提取图片尺寸
    img_width: int | None = None
    img_height: int | None = None
    try:
        content = storage_service.get_file(data.file_path)
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(content))
        img_width, img_height = img.size
    except Exception:
        pass

    # 校验图片尺寸
    if img_width is not None and ps.width > 0 and img_width != ps.width:
        raise BusinessException(ErrorCode.IMAGE_WIDTH_NOT_MEET, get_msg("IMAGE_WIDTH_NOT_MEET"))
    if img_height is not None and ps.height > 0 and img_height != ps.height:
        raise BusinessException(ErrorCode.IMAGE_HEIGHT_NOT_MEET, get_msg("IMAGE_HEIGHT_NOT_MEET"))

    # 同一实体 + 同一海报规格只能保留一张：软删除旧记录（保留文件）
    old_pics = (
        await db.execute(
            select(Picture).where(
                Picture.entity_type == data.entity_type,
                Picture.entity_id == data.entity_id,
                Picture.poster_size_id == data.poster_size_id,
                Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
            )
        )
    ).scalars().all()
    for old in old_pics:
        old.is_deleted = True
    if old_pics:
        await db.commit()

    pic = Picture(
        entity_type=data.entity_type,
        entity_id=data.entity_id,
        poster_size_id=data.poster_size_id,
        file_name=data.file_name,
        file_path=data.file_path,
        file_size=data.file_size,
        width=img_width,
        height=img_height,
    )
    db.add(pic)
    await db.commit()
    await db.refresh(pic)

    # 如果是内容类实体上传海报，完成 Posters 流程节点
    if data.entity_type in CONTENT_ENTITY_TYPES:
        content = await get_content_by_id(db, data.entity_id)
        if content:
            await complete_process_and_update_status(
                db,
                content_id=data.entity_id,
                content_type=content.content_type,
                process_name="Posters",
                processed_by=current_user.username,
                info=f"上传海报: {data.file_name}",
            )

    import json
    poster_name = ps.name if ps else data.file_name
    upd_val = json.dumps({"poster_name": poster_name, "file_name": data.file_name}, ensure_ascii=False)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.POSTER_UPLOAD,
        operation_object=f"海报 {poster_name}",
        operation_content="log.poster.upload",
        content_id=_content_id_for_picture(data.entity_type, data.entity_id),
        entity_type=data.entity_type,
        entity_id=data.entity_id,
        previous_value=None,
        updated_value=upd_val,
        updated_value_json=None,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return _to_item(pic)


@router.post("/pictures/upload", response_model=PictureItem)
async def upload_picture(
    entity_type: str = Query(...),
    entity_id: int = Query(...),
    poster_size_id: int = Query(...),
    file: UploadFile = ...,
    request: Request = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    ps = (await db.execute(select(PosterSize).where(PosterSize.id == poster_size_id, PosterSize.is_deleted.is_(False)))).scalar_one_or_none()
    if not ps:
        raise BusinessException(ErrorCode.POSTER_SIZE_NOT_FOUND, get_msg("POSTER_SIZE_NOT_FOUND"))

    # 校验扩展名
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    allowed_exts = [e.extension.lower() for e in ps.extensions]
    if allowed_exts and ext not in allowed_exts:
        raise BusinessException(ErrorCode.INVALID_FILE_FORMAT, get_msg("INVALID_FILE_FORMAT"))

    content = await file.read()

    # 校验文件大小
    if ps.max_file_size_kb > 0 and len(content) > ps.max_file_size_kb * 1024:
        raise BusinessException(ErrorCode.FILE_TOO_LARGE, get_msg("FILE_TOO_LARGE"))

    # 获取图片尺寸
    img_width: int | None = None
    img_height: int | None = None
    try:
        from PIL import Image
        from io import BytesIO
        img = Image.open(BytesIO(content))
        img_width, img_height = img.size
    except Exception:
        pass

    # 校验图片尺寸
    if img_width is not None and ps.width > 0 and img_width != ps.width:
        raise BusinessException(ErrorCode.IMAGE_WIDTH_NOT_MEET, get_msg("IMAGE_WIDTH_NOT_MEET"))
    if img_height is not None and ps.height > 0 and img_height != ps.height:
        raise BusinessException(ErrorCode.IMAGE_HEIGHT_NOT_MEET, get_msg("IMAGE_HEIGHT_NOT_MEET"))

    saved = storage_service.save_file(content, file.filename or "upload", f"pictures/{entity_type}/{entity_id}")

    pic = Picture(
        entity_type=entity_type,
        entity_id=entity_id,
        poster_size_id=poster_size_id,
        file_name=saved["file_name"],
        file_path=saved["file_path"],
        file_size=saved["file_size"],
        width=img_width,
        height=img_height,
    )
    db.add(pic)
    await db.commit()
    await db.refresh(pic)

    # 如果是内容类实体上传海报，完成 Posters 流程节点
    if entity_type in CONTENT_ENTITY_TYPES:
        content = await get_content_by_id(db, entity_id)
        if content:
            await complete_process_and_update_status(
                db,
                content_id=entity_id,
                content_type=content.content_type,
                process_name="Posters",
                processed_by=current_user.username,
                info=f"上传海报: {saved['file_name']}",
            )

    import json
    poster_name = ps.name if ps else saved["file_name"]
    upd_val = json.dumps({"poster_name": poster_name, "file_name": saved["file_name"]}, ensure_ascii=False)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.POSTER_UPLOAD,
        operation_object=f"海报 {poster_name}",
        operation_content="log.poster.upload",
        content_id=_content_id_for_picture(entity_type, entity_id),
        entity_type=entity_type,
        entity_id=entity_id,
        previous_value=None,
        updated_value=upd_val,
        updated_value_json=None,
        ip_address=_get_ip(request) if request else None,
        result="success",
    )
    await db.commit()
    return _to_item(pic)


@router.delete("/pictures/{picture_id}")
async def delete_picture(
    picture_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    pic = (await db.execute(select(Picture).where(Picture.id == picture_id, Picture.is_deleted.is_(False), Picture.is_discarded.is_(False)))).scalar_one_or_none()
    if not pic:
        raise BusinessException(ErrorCode.PICTURE_NOT_FOUND, get_msg("PICTURE_NOT_FOUND"))
    # 软删除：保留文件，仅标记 is_deleted
    pic.is_deleted = True
    await db.commit()
    import json
    ps = (await db.execute(select(PosterSize).where(PosterSize.id == pic.poster_size_id))).scalar_one_or_none()
    poster_name = ps.name if ps else pic.file_name
    prev_val = json.dumps({"poster_name": poster_name, "file_name": pic.file_name}, ensure_ascii=False)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.POSTER_DELETE,
        operation_object=f"海报 {poster_name}",
        operation_content="log.poster.delete",
        content_id=_content_id_for_picture(pic.entity_type, pic.entity_id),
        entity_type=pic.entity_type,
        entity_id=pic.entity_id,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=None,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/files/{file_path:path}")
async def serve_file(
    file_path: str,
    _: User = Depends(get_current_user),
):
    """代理下载文件（本地存储或 SFTP）"""
    from urllib.parse import unquote
    from app.config import settings

    # FastAPI 会自动解码 path 参数，但中文文件名可能被二次编码，统一 unquote 一次
    decoded_path = unquote(file_path)
    filename = decoded_path.rsplit("/", 1)[-1]

    if settings.sftp_enabled and settings.sftp_host:
        import posixpath, paramiko
        from io import BytesIO
        remote_path = posixpath.join(settings.sftp_base_path, decoded_path)
        transport = paramiko.Transport((settings.sftp_host, settings.sftp_port))
        try:
            transport.connect(username=settings.sftp_username, password=settings.sftp_password)
            client = paramiko.SFTPClient.from_transport(transport)
            buf = BytesIO()
            client.getfo(remote_path, buf)
            buf.seek(0)
            from urllib.parse import quote
            encoded_filename = quote(filename, safe='')
            return StreamingResponse(
                buf,
                media_type="application/octet-stream",
                headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"},
            )
        except FileNotFoundError:
            raise NotFoundException(ErrorCode.FILE_NOT_FOUND, get_msg("FILE_NOT_FOUND"))
        except Exception as e:
            raise BusinessException(ErrorCode.FILE_READ_ERROR, get_msg("FILE_READ_ERROR", error=str(e)))
        finally:
            transport.close()
    else:
        import os
        from fastapi.responses import FileResponse
        from app.internal.cms_biz_orchestration.services.storage import LOCAL_UPLOAD_DIR
        local_path = os.path.join(LOCAL_UPLOAD_DIR, *decoded_path.replace("/", os.sep).split(os.sep))
        if not os.path.exists(local_path):
            raise NotFoundException(ErrorCode.FILE_NOT_FOUND, get_msg("FILE_NOT_FOUND"))
        return FileResponse(
            local_path,
            media_type="application/octet-stream",
            filename=filename,
        )
