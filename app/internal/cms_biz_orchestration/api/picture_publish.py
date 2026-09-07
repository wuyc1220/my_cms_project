"""
海报发布模块 —— 只发布 Picture 对象，生成 C2 规范的 Picture XML。

本模块提供独立的海报发布功能：
- 生成只包含 Picture 对象的 C2 XML
- 上传 XML 到 SFTP
- 调用 SOAP 服务通知 LSP
- 创建 IngestHistory 记录
"""
from datetime import datetime, timezone
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

import uuid
from fastapi import APIRouter, Depends, Body, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException, ErrorCode
from app.common.core.i18n import get_msg
from app.common.dependencies import get_current_user, _get_ip, get_db
from app.internal.cms_biz_metada.models.basic import Picture, PosterSize
from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.soap.c2.constants import Action, ElementType
from app.soap.c2.objects import build_picture_object
from app.soap.c2.mappings import add_mapping
from app.soap.c2.file_utils import generate_c2_filename, upload_c2_xml
from app.soap.config import soap_settings
from loguru import logger

router = APIRouter()


class PicturePublishResponse(BaseModel):
    """海报发布响应"""
    success: bool
    message: str
    xml_path: str | None = None


class PicturePublishRequest(BaseModel):
    """海报发布请求"""
    entity_type: str
    entity_id: int
    model_config = {"json_schema_extra": {"example": {"entity_type": "schedule", "entity_id": 1}}}


def _build_picture_only_root() -> Element:
    """生成 <ADI> 根节点（仅用于 Picture 发布）"""
    root = Element(
        "ADI",
        {
            "xmlns": "http://www.ChinaDTV.cn/CDTVStandard/ADI",
            "Version": "1.0",
        },
    )
    header = SubElement(root, "Header")
    SubElement(header, "MsgID").text = str(uuid.uuid4())
    SubElement(header, "CSPID").text = soap_settings.csp_id
    SubElement(header, "LSPID").text = soap_settings.lsp_id
    SubElement(header, "Timestamp").text = datetime.now().isoformat()
    return root


def _serialize(root: Element) -> str:
    """序列化 XML 并添加声明"""
    rough = tostring(root, encoding="unicode")
    reparsed = minidom.parseString(rough.encode("utf-8"))
    return reparsed.toprettyxml(indent="  ", encoding="utf-8").decode("utf-8")


async def _generate_picture_only_xml(
    entity_type: str,
    entity_id: int,
    pictures: list[Picture],
    poster_size_map: dict[int, PosterSize],
    picture_custom_fields: dict[int, dict] | None = None,
) -> str:
    """
    生成只包含 Picture 对象的 C2 XML。
    
    生成的 XML 结构：
    - Objects: 包含所有 Picture 对象
    - Mappings: 包含 Picture 到父实体的映射
    """
    root = _build_picture_only_root()
    objects_el = SubElement(root, "Objects")
    mappings_el = SubElement(root, "Mappings")

    # 确定父实体类型
    parent_element_type_map = {
        "schedule": ElementType.SCHEDULE,
        "channel": ElementType.CHANNEL,
        "program": ElementType.PROGRAM,
        "series": ElementType.SERIES,
    }
    parent_element_type = parent_element_type_map.get(entity_type.lower())

    pic_custom_fields = picture_custom_fields or {}

    for pic in pictures:
        poster_size = poster_size_map.get(pic.poster_size_id)
        poster_size_name = poster_size.name if poster_size else None

        # 构建 Picture 对象（扩展字段取自所属海报规格的自定义字段）
        pic_obj = build_picture_object(
            pic=pic,
            action=Action.REGIST,
            poster_size_name=poster_size_name,
            custom_fields=pic_custom_fields.get(pic.poster_size_id, {}),
        )
        objects_el.append(pic_obj)

        # 添加 Picture 到父实体的映射
        if parent_element_type:
            add_mapping(
                mappings_el,
                ElementType.PICTURE,
                pic.id,
                parent_element_type,
                entity_id,
                Action.REGIST,
            )

    return _serialize(root)


async def _upload_xml_to_storage(xml_content: str, entity_type: str, entity_id: int) -> str:
    """上传 XML 到存储并返回文件路径

    文件路径格式: c2/YYYYMM/pictures_entity_type_entity_id_uuid_timestamp.xml
    示例: c2/202601/pictures_schedule_178_8a9f3bbe_20260122_102335.xml
    """
    filename = generate_c2_filename(
        prefix="pictures",
        entity_type=entity_type,
        entity_id=entity_id,
    )
    return upload_c2_xml(xml_content, filename, storage_service)


async def _send_soap_notification(xml_path: str) -> dict:
    """发送 SOAP 通知给 LSP"""
    from app.soap.client import SOAPClient

    xml_url = storage_service.get_file_url(xml_path)
    correlate_id = uuid.uuid4().hex

    try:
        soap_client = SOAPClient()
        result = soap_client.send_exec_cmd_req(
            cmd_file_url=xml_url,
            correlate_id=correlate_id,
        )
        return {
            "success": result.get("success", False),
            "correlate_id": correlate_id,
            "result": result.get("result"),
            "error": result.get("error_description"),
        }
    except Exception as e:
        return {
            "success": False,
            "correlate_id": correlate_id,
            "error": str(e),
        }


@router.post("/pictures/publish", response_model=PicturePublishResponse)
async def publish_pictures(
    request: Request,
    data: PicturePublishRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    发布指定实体的所有海报。

    只生成包含 Picture 对象的 XML，不发布内容本身。
    用于在发布节目单/频道前确保海报已发布到 C2。
    """
    # 1. 查询该实体下的所有有效海报
    pictures = (
        await db.execute(
            select(Picture)
            .where(
                Picture.entity_type == data.entity_type,
                Picture.entity_id == data.entity_id,
                Picture.is_deleted.is_(False),
                Picture.is_discarded.is_(False),
            )
            .order_by(Picture.created_at)
        )
    ).scalars().all()

    if not pictures:
        raise BusinessException(
            ErrorCode.NO_PICTURES_TO_PUBLISH,
            get_msg("NO_PICTURES_TO_PUBLISH")
        )

    # 1.5 校验必填海报规格是否全部上传（与前端海报弹窗一致：belonging 匹配）
    applicable_sizes = (
        await db.execute(
            select(PosterSize).where(PosterSize.is_deleted.is_(False))
        )
    ).scalars().all()
    belonging_map = {
        "program": "Program", "series": "Series",
        "channel": "Channel", "schedule": "Schedule",
    }
    belonging = belonging_map.get(data.entity_type.lower(), "Program")
    applicable = [
        ps for ps in applicable_sizes
        if any(b.belonging == belonging or b.belonging == "ALL" for b in (ps.belongings or []))
    ]
    mandatory_sizes = [ps for ps in applicable if ps.mandatory]
    if mandatory_sizes:
        uploaded_size_ids = {p.poster_size_id for p in pictures}
        missing = [ps.name for ps in mandatory_sizes if ps.id not in uploaded_size_ids]
        if missing:
            raise BusinessException(
                ErrorCode.MANDATORY_POSTERS_MISSING,
                get_msg("MANDATORY_POSTERS_MISSING", names=", ".join(missing))
            )

    # 2. 获取 PosterSize 信息用于 Description
    poster_size_ids = [p.poster_size_id for p in pictures]
    poster_sizes = (
        await db.execute(
            select(PosterSize)
            .where(PosterSize.id.in_(poster_size_ids), PosterSize.is_deleted.is_(False))
        )
    ).scalars().all()
    poster_size_map = {ps.id: ps for ps in poster_sizes}

    # 2.5 加载海报规格上的自定义字段（Picture 的扩展字段挂在 PosterSize 上）
    from app.soap.c2.loader import _load_custom_fields_for_entities, _load_languages
    languages, primary_language = await _load_languages(db)
    picture_custom_fields = await _load_custom_fields_for_entities(
        db, "poster_size", list(poster_size_map.keys()), languages, primary_language
    )

    # 3. 生成只包含 Picture 的 XML
    try:
        xml_content = await _generate_picture_only_xml(
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            pictures=pictures,
            poster_size_map=poster_size_map,
            picture_custom_fields=picture_custom_fields,
        )
    except Exception as e:
        raise BusinessException(
            ErrorCode.PICTURE_PUBLISH_FAILED,
            get_msg("PICTURE_PUBLISH_FAILED", error=str(e))
        )

    # 4. 上传 XML 到存储
    try:
        xml_path = await _upload_xml_to_storage(xml_content, data.entity_type, data.entity_id)
    except Exception as e:
        raise BusinessException(
            ErrorCode.PICTURE_PUBLISH_FAILED,
            get_msg("PICTURE_PUBLISH_UPLOAD_FAILED", error=str(e))
        )

    # 5. 发送 SOAP 通知（仅在启用时）
    logger.info(f"SOAP 开关状态: enabled={soap_settings.enabled}")
    if soap_settings.enabled:
        soap_result = await _send_soap_notification(xml_path)
        logger.info(f"SOAP 调用结果: {soap_result}")
    else:
        logger.info("SOAP 调用已跳过（SOAP_ENABLED=false）")
        soap_result = {
            "success": True,
            "correlate_id": None,
            "result": None,
            "error": None,
        }

    # 6. 更新 Picture 的发布状态，创建 IngestHistory 记录，并保存到 object_publish_status 表
    from sqlalchemy import update
    from datetime import datetime, timezone
    from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory
    from app.internal.cms_biz_publish.repositories import publish_repository
    
    current_time = datetime.now(timezone.utc)
    ingest_status = "success" if soap_result["success"] else "failed"
    last_action = "REGIST"  # 统一使用 REGIST
    
    # 6.1 更新 Picture 的 ingest_status 字段
    for pic in pictures:
        await db.execute(
            update(Picture)
            .where(Picture.id == pic.id)
            .values(
                ingest_status=ingest_status,
                updated_at=current_time,
            )
        )
    
    # 6.2 创建 IngestHistory 记录保存详细信息
    for pic in pictures:
        history = IngestHistory(
            entity_type="Picture",
            entity_id=pic.id,
            action="REGIST",
            status="success" if soap_result["success"] else "failure",
            ingest_xml_path=xml_path,
            correlate_id=soap_result.get("correlate_id"),
            soap_error_description=soap_result.get("error") if not soap_result["success"] else None,
            send_date=current_time,
        )
        db.add(history)
    
    # 6.3 保存到 object_publish_status 表（统一使用 REGIST）
    for pic in pictures:
        obj_status = await publish_repository.get_or_create_object_publish_status(
            db, "Picture", pic.id, content_id=None,
        )
        obj_status.mark_as_published(last_action)
        await publish_repository.update_object_publish_status(db, obj_status)
    
    await db.commit()
    logger.info(f"已更新 {len(pictures)} 张海报的发布状态为: {ingest_status}，并创建了 IngestHistory 和 ObjectPublishStatus 记录")

    # 7. 记录操作日志
    import json
    pic_info = [
        {
            "id": p.id,
            "file_name": p.file_name,
            "poster_size_id": p.poster_size_id,
        }
        for p in pictures
    ]
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.PICTURE_PUBLISH,
        operation_object_code="OBJ_POSTER", operation_object_params={"name": data.entity_type},
        operation_content_code="LOG_PICTURE_PUBLISH",
        content_id=data.entity_id if data.entity_type in ("schedule", "channel", "program") else None,
        entity_type=data.entity_type,
        entity_id=data.entity_id,
        previous_value=None,
        updated_value=json.dumps({
            "pictures": pic_info,
            "xml_path": xml_path,
            "soap_success": soap_result["success"],
            "correlate_id": soap_result.get("correlate_id"),
        }, ensure_ascii=False),
        # 原始入参快照（发布请求与 SOAP 结果）
        updated_value_json=json.dumps({
            "entity_type": data.entity_type,
            "entity_id": data.entity_id,
            "picture_count": len(pictures),
            "xml_path": xml_path,
            "soap_success": soap_result["success"],
            "correlate_id": soap_result.get("correlate_id"),
        }, ensure_ascii=False, default=str),
        ip_address=_get_ip(request),
        result="success" if soap_result["success"] else "failure",
    )
    await db.commit()

    if not soap_result["success"]:
        raise BusinessException(
            ErrorCode.PICTURE_PUBLISH_FAILED,
            get_msg("PICTURE_PUBLISH_SOAP_FAILED", error=soap_result.get("error", "Unknown"))
        )

    return PicturePublishResponse(
        success=True,
        message=get_msg("PICTURE_PUBLISH_SUCCESS"),
        xml_path=xml_path,
    )
