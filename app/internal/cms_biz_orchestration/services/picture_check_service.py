"""
海报发布状态校验服务

用于在节目单/频道发布前校验海报是否已发布
"""
from typing import List, Optional, Tuple
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import Picture
from app.common.core.exceptions import BusinessException, ErrorCode
from app.common.core.i18n import get_msg


class PictureCheckResult:
    """海报校验结果"""
    def __init__(
        self,
        can_publish: bool,
        total_count: int,
        published_count: int,
        unpublished_count: int,
        unpublished_pictures: List[dict],
        message: str = "",
    ):
        self.can_publish = can_publish
        self.total_count = total_count
        self.published_count = published_count
        self.unpublished_count = unpublished_count
        self.unpublished_pictures = unpublished_pictures
        self.message = message


# 需要校验海报发布状态的内容类型（大写，与 content_type 一致）
CHECK_REQUIRED_CONTENT_TYPES = {"SCHEDULE", "CHANNEL"}

# content_type 大写 -> Picture.entity_type 小写 的映射
CONTENT_TYPE_TO_ENTITY_TYPE = {
    "SCHEDULE": "schedule",
    "CHANNEL": "channel",
}


async def check_pictures_before_publish(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_type: Optional[str] = None,
    raise_exception: bool = True,
) -> PictureCheckResult:
    """
    在发布内容前校验海报是否已发布

    只对 SCHEDULE 和 CHANNEL 类型的实体进行校验

    Args:
        db: 数据库会话
        entity_type: 实体类型（发布系统中统一为 "Content"）
        entity_id: 实体ID
        content_type: 内容类型（SCHEDULE/CHANNEL/PROGRAM 等，大写）
        raise_exception: 如果校验失败是否抛出异常

    Returns:
        PictureCheckResult: 校验结果

    Raises:
        BusinessException: 如果 raise_exception=True 且校验失败
    """
    # 1. 检查是否需要校验（只有 SCHEDULE 和 CHANNEL 需要）
    effective_content_type = content_type or ""
    if effective_content_type.upper() not in CHECK_REQUIRED_CONTENT_TYPES:
        logger.info(
            f"[海报校验] 跳过校验: content_type={content_type}, "
            f"不在需要校验的类型列表中 {CHECK_REQUIRED_CONTENT_TYPES}"
        )
        return PictureCheckResult(
            can_publish=True,
            total_count=0,
            published_count=0,
            unpublished_count=0,
            unpublished_pictures=[],
            message=f"content_type={content_type} 不需要校验海报发布状态",
        )

    # 2. 将 content_type 映射为 Picture 表中的 entity_type（小写）
    picture_entity_type = CONTENT_TYPE_TO_ENTITY_TYPE.get(effective_content_type.upper())
    if not picture_entity_type:
        logger.warning(f"[海报校验] 未知的 content_type: {content_type}，跳过校验")
        return PictureCheckResult(
            can_publish=True,
            total_count=0,
            published_count=0,
            unpublished_count=0,
            unpublished_pictures=[],
            message=f"未知的 content_type={content_type}，跳过校验",
        )

    # 3. 查询该实体下的所有有效海报（排除已删除和已废弃的）
    stmt = select(Picture).where(
        Picture.entity_type == picture_entity_type,
        Picture.entity_id == entity_id,
        Picture.is_deleted == False,
        Picture.is_discarded == False,
    )
    result = await db.execute(stmt)
    pictures = result.scalars().all()

    logger.info(
        f"[海报校验] 查询海报: entity_type={picture_entity_type}, entity_id={entity_id}, "
        f"找到 {len(pictures)} 张海报"
    )

    # 4. 没有海报的情况
    if not pictures:
        logger.info(f"[海报校验] entity_id={entity_id} 没有海报，允许发布")
        return PictureCheckResult(
            can_publish=True,
            total_count=0,
            published_count=0,
            unpublished_count=0,
            unpublished_pictures=[],
            message="该实体下没有海报，可以直接发布",
        )

    # 5. 检查每张海报的发布状态
    unpublished_pictures = []
    published_count = 0

    for pic in pictures:
        logger.debug(
            f"[海报校验] 海报 id={pic.id}, file_name={pic.file_name}, "
            f"ingest_status={pic.ingest_status}"
        )
        if pic.ingest_status == "success":
            published_count += 1
        else:
            unpublished_pictures.append({
                "id": pic.id,
                "file_name": pic.file_name,
                "ingest_status": pic.ingest_status,
                "message": _get_status_message(pic.ingest_status),
            })

    unpublished_count = len(unpublished_pictures)
    can_publish = unpublished_count == 0

    # 6. 构建结果
    check_result = PictureCheckResult(
        can_publish=can_publish,
        total_count=len(pictures),
        published_count=published_count,
        unpublished_count=unpublished_count,
        unpublished_pictures=unpublished_pictures,
        message=_build_result_message(can_publish, len(pictures), published_count, unpublished_count),
    )

    logger.info(
        f"[海报校验] 校验结果: can_publish={can_publish}, "
        f"total={len(pictures)}, published={published_count}, unpublished={unpublished_count}, "
        f"message={check_result.message}"
    )

    # 7. 如果需要且校验失败，抛出异常
    if raise_exception and not can_publish:
        unpublished_names = [p["file_name"] for p in unpublished_pictures[:3]]
        logger.warning(
            f"[海报校验] 阻止发布! entity_id={entity_id}, "
            f"有 {unpublished_count}/{len(pictures)} 张海报未发布, "
            f"未发布海报: {unpublished_names}"
        )
        raise BusinessException(
            ErrorCode.PICTURES_NOT_PUBLISHED,
            get_msg("PICTURES_NOT_PUBLISHED"),
        )

    return check_result


def _get_status_message(status: str) -> str:
    """获取状态描述"""
    status_map = {
        "none": "未发布",
        "processing": "发布中",
        "success": "发布成功",
        "failed": "发布失败",
    }
    return status_map.get(status, f"未知状态: {status}")


def _build_result_message(
    can_publish: bool,
    total: int,
    published: int,
    unpublished: int,
) -> str:
    """构建结果消息"""
    if can_publish:
        if total == 0:
            return "该实体下没有海报，可以直接发布"
        else:
            return f"全部 {total} 张海报已发布成功，可以发布内容"
    else:
        return f"有 {unpublished}/{total} 张海报未发布成功，请先发布海报"


async def can_publish_content(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_type: Optional[str] = None,
) -> bool:
    """
    快速检查是否可以发布内容（只返回布尔值，不抛出异常）

    只对 SCHEDULE 和 CHANNEL 类型进行校验
    """
    effective_content_type = content_type or ""
    if effective_content_type.upper() not in CHECK_REQUIRED_CONTENT_TYPES:
        return True

    result = await check_pictures_before_publish(
        db, entity_type, entity_id, content_type=content_type, raise_exception=False
    )
    return result.can_publish
