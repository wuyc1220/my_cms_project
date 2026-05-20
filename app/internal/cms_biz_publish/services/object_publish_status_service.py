"""
对象发布状态管理服务。

提供发布状态的查询、更新、批量操作等业务逻辑。
"""
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_publish.models.object_publish_status import ObjectPublishStatus
from app.internal.cms_biz_publish.repositories import publish_repository
from app.database import Base


_ENTITY_TYPE_INGEST_TABLE_MAP: dict[str, str] = {
    "Cast": "cast",
    "Category": "category",
    "Package": "package",
    "Movie": "movie",
    "PhysicalChannel": "physical_channel",
}


# ═══════════════════════════════════════════════════════════
# Action 判断逻辑
# ═══════════════════════════════════════════════════════════

async def decide_action_for_object(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_id: Optional[int] = None,
    is_unpublish: bool = False,
    obj_updated_at: Optional[datetime] = None,
) -> Optional[str]:
    """
    基于发布历史判断对象的Action。
    
    核心逻辑：
    - 下架任务且是主对象 → DELETE
    - 下架任务但是关联对象 → None（跳过，不处理）
    - 曾经发布成功过且无变更 → SKIP（仅输出Mapping，不输出Object）
    - 曾经发布成功过且有变更 → UPDATE
    - 从未发布过 → REGIST
    
    变更检测：比较 obj_updated_at 与 last_publish_time，
    若 obj_updated_at <= last_publish_time 则判定为无变更。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型（Content/Cast/Package/Picture/Movie/Category）
        entity_id: 实体ID
        content_id: 关联的主内容ID
        is_unpublish: 是否为下架任务
        obj_updated_at: ORM对象的updated_at时间，用于变更检测
    
    Returns:
        "REGIST" / "UPDATE" / "DELETE" / "SKIP" / None（跳过）
    """
    # 1. 下架任务处理
    if is_unpublish:
        if entity_type == "Content":
            return "DELETE"
        else:
            logger.debug(
                f"关联对象 {entity_type}#{entity_id} 在下架流程中跳过，不生成DELETE"
            )
            return None
    
    # 2. 查询发布历史
    history = await publish_repository.get_object_publish_status(
        db, entity_type, entity_id
    )
    
    logger.info(
        f"[decide_action] {entity_type}#{entity_id} | "
        f"content_id={content_id} | obj_updated_at={obj_updated_at} | "
        f"history_exists={history is not None} | "
        f"is_published={history.is_published if history else None} | "
        f"last_publish_time={history.last_publish_time if history else None}"
    )
    
    # 3. 判断Action
    if history and history.is_published:
        # 曾经发布成功过，检查是否有变更
        if (
            obj_updated_at is not None
            and history.last_publish_time is not None
            and obj_updated_at <= history.last_publish_time
        ):
            # 主对象（Content）不能SKIP，必须输出Object
            if entity_type == "Content":
                logger.info(f"[decide_action] {entity_type}#{entity_id} → UPDATE (主对象不能SKIP)")
                return "UPDATE"
            
            time_diff = history.last_publish_time - obj_updated_at
            logger.info(
                f"[decide_action] {entity_type}#{entity_id} → SKIP | "
                f"updated_at({obj_updated_at}) <= last_publish_time({history.last_publish_time}) | "
                f"差值={time_diff}"
            )
            return "SKIP"
        
        if obj_updated_at and history.last_publish_time:
            time_diff = obj_updated_at - history.last_publish_time
            logger.info(
                f"[decide_action] {entity_type}#{entity_id} → UPDATE | "
                f"updated_at({obj_updated_at}) > last_publish_time({history.last_publish_time}) | "
                f"差值={time_diff}"
            )
        else:
            logger.info(f"[decide_action] {entity_type}#{entity_id} → UPDATE (时间字段不完整)")
        
        return "UPDATE"
    else:
        logger.info(f"[decide_action] {entity_type}#{entity_id} → REGIST (未发布过)")
        return "REGIST"


# ═══════════════════════════════════════════════════════════
# 状态更新逻辑
# ═══════════════════════════════════════════════════════════

async def mark_object_as_published(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_id: Optional[int] = None,
    action: str = "REGIST",
) -> ObjectPublishStatus:
    """
    标记对象为发布成功。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型
        entity_id: 实体ID
        content_id: 关联的主内容ID
        action: REGIST 或 UPDATE
    
    Returns:
        更新后的 ObjectPublishStatus
    """
    status = await publish_repository.get_or_create_object_publish_status(
        db, entity_type, entity_id, content_id
    )
    
    status.mark_as_published(action)
    await publish_repository.update_object_publish_status(db, status)
    
    logger.info(
        f"对象发布成功 | {entity_type}#{entity_id} | action={action} | "
        f"first_publish={status.first_publish_time}"
    )
    
    return status


async def mark_object_as_unpublished(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
) -> Optional[ObjectPublishStatus]:
    """
    标记对象为已下架（仅主对象使用）。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型（应该是Content）
        entity_id: 实体ID
    
    Returns:
        ObjectPublishStatus 或 None（如果不是主对象）
    """
    # 只有主对象才能下架
    if entity_type != "Content":
        logger.warning(
            f"关联对象 {entity_type}#{entity_id} 不支持下架操作"
        )
        return None
    
    status = await publish_repository.get_object_publish_status(
        db, entity_type, entity_id
    )
    
    if status is None:
        logger.warning(
            f"对象 {entity_type}#{entity_id} 没有发布状态记录，无法下架"
        )
        return None
    
    status.mark_as_unpublished()
    await publish_repository.update_object_publish_status(db, status)
    
    logger.info(
        f"对象已下架 | {entity_type}#{entity_id} | "
        f"unpublish_time={status.last_unpublish_time}"
    )
    
    return status


async def mark_object_as_failed(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_id: Optional[int] = None,
) -> ObjectPublishStatus:
    """
    标记对象发布失败。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型
        entity_id: 实体ID
        content_id: 关联的主内容ID
    
    Returns:
        更新后的 ObjectPublishStatus
    """
    status = await publish_repository.get_or_create_object_publish_status(
        db, entity_type, entity_id, content_id
    )
    
    status.mark_as_failed()
    await publish_repository.update_object_publish_status(db, status)
    
    logger.warning(
        f"对象发布失败 | {entity_type}#{entity_id} | "
        f"last_action={status.last_action}"
    )
    
    return status


# ═══════════════════════════════════════════════════════════
# 关联对象 ingest_status 同步
# ═══════════════════════════════════════════════════════════

async def _sync_object_ingest_status(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    ingest_status: str,
) -> None:
    table_name = _ENTITY_TYPE_INGEST_TABLE_MAP.get(entity_type)
    if table_name is None:
        return
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return
    await db.execute(
        sa_update(table)
        .where(table.c.id == entity_id)
        .values(ingest_status=ingest_status)
    )


# ═══════════════════════════════════════════════════════════
# 批量操作
# ═══════════════════════════════════════════════════════════

async def batch_mark_objects_as_published(
    db: AsyncSession,
    objects: list[dict],
    content_id: int,
    action: str = "REGIST",  # 保留参数以兼容旧代码，但不再使用
) -> None:
    """
    批量标记对象为发布成功。
    
    Args:
        db: 数据库会话
        objects: 对象列表，每个对象包含 {"entity_type": str, "entity_id": int, "action": str}
        content_id: 关联的主内容ID
        action: 保留参数，不再使用（每个对象使用自己的 action）
    """
    for obj in objects:
        entity_type = obj["entity_type"]
        entity_id = obj["entity_id"]
        obj_action = obj.get("action", action)  # 使用对象自己的 action，如果没有则使用默认值
        
        await mark_object_as_published(
            db, entity_type, entity_id, content_id, obj_action
        )
    
    logger.info(
        f"批量标记 {len(objects)} 个对象为发布成功 | content_id={content_id}"
    )


async def batch_mark_objects_from_context(
    db: AsyncSession,
    content_id: int,
    ctx,  # BuildContext
    action: str = "REGIST",
    ingest_history_id: Optional[int] = None,
) -> None:
    """
    从BuildContext批量标记所有涉及的对象为发布成功。

    同时完成：
    1. 更新 object_publish_status 表（仅非 SKIP 对象更新 last_publish_time）
    2. 同步关联对象的 ingest_status 字段
    3. 写入 ingest_history_detail 明细记录

    Args:
        db: 数据库会话
        content_id: 主内容ID
        ctx: BuildContext（包含所有关联对象）
        action: REGIST 或 UPDATE
        ingest_history_id: 关联的 IngestHistory 主记录ID
    """
    from app.internal.cms_biz_orchestration.models.ingest_history_detail import IngestHistoryDetail

    objects_to_mark = []
    detail_records = []

    # 主对象（Content）- 主对象不 SKIP
    # 判断主对象的 Action
    main_obj_action = await _decide_action_for_related_object(db, "Content", content_id, content_id, 
                                                               getattr(ctx.content, 'updated_at', None))
    objects_to_mark.append({
        "entity_type": "Content",
        "entity_id": content_id,
        "action": main_obj_action,  # 使用判断出的 Action
    })

    # Movies
    for movie in getattr(ctx, 'movies', []):
        obj_action = await _decide_action_for_related_object(db, "Movie", movie.id, content_id, movie.updated_at)
        if obj_action != "SKIP":
            objects_to_mark.append({
                "entity_type": "Movie",
                "entity_id": movie.id,
                "action": obj_action,  # 使用判断出的 Action
            })
            if ingest_history_id:
                detail_records.append(IngestHistoryDetail(
                    history_id=ingest_history_id,
                    entity_type="Movie",
                    entity_id=movie.id,
                    entity_name=getattr(movie, 'name', None) or getattr(movie, 'file_path', None),
                    action=obj_action,
                ))
        await _sync_object_ingest_status(db, "Movie", movie.id, "success")

    # Casts
    for cast in getattr(ctx, 'casts', []):
        obj_action = await _decide_action_for_related_object(db, "Cast", cast.id, content_id, cast.updated_at)
        if obj_action != "SKIP":
            objects_to_mark.append({
                "entity_type": "Cast",
                "entity_id": cast.id,
                "action": obj_action,  # 使用判断出的 Action
            })
            if ingest_history_id:
                detail_records.append(IngestHistoryDetail(
                    history_id=ingest_history_id,
                    entity_type="Cast",
                    entity_id=cast.id,
                    entity_name=getattr(cast, 'name', None),
                    action=obj_action,
                ))
        await _sync_object_ingest_status(db, "Cast", cast.id, "success")

    # Categories
    for cat in getattr(ctx, 'categories', []):
        obj_action = await _decide_action_for_related_object(db, "Category", cat.id, content_id, cat.updated_at)
        if obj_action != "SKIP":
            objects_to_mark.append({
                "entity_type": "Category",
                "entity_id": cat.id,
                "action": obj_action,  # 使用判断出的 Action
            })
            if ingest_history_id:
                detail_records.append(IngestHistoryDetail(
                    history_id=ingest_history_id,
                    entity_type="Category",
                    entity_id=cat.id,
                    entity_name=getattr(cat, 'name', None),
                    action=obj_action,
                ))
        await _sync_object_ingest_status(db, "Category", cat.id, "success")

    # Packages
    for pkg in getattr(ctx, 'packages', []):
        obj_action = await _decide_action_for_related_object(db, "Package", pkg.id, content_id, pkg.updated_at)
        if obj_action != "SKIP":
            objects_to_mark.append({
                "entity_type": "Package",
                "entity_id": pkg.id,
                "action": obj_action,  # 使用判断出的 Action
            })
            if ingest_history_id:
                detail_records.append(IngestHistoryDetail(
                    history_id=ingest_history_id,
                    entity_type="Package",
                    entity_id=pkg.id,
                    entity_name=getattr(pkg, 'name', None),
                    action=obj_action,
                ))
        await _sync_object_ingest_status(db, "Package", pkg.id, "success")

    # Pictures
    for pic in getattr(ctx, 'pictures', []):
        obj_action = await _decide_action_for_related_object(db, "Picture", pic.id, content_id, pic.updated_at)
        if obj_action != "SKIP":
            objects_to_mark.append({
                "entity_type": "Picture",
                "entity_id": pic.id,
                "action": obj_action,  # 使用判断出的 Action
            })
            if ingest_history_id:
                detail_records.append(IngestHistoryDetail(
                    history_id=ingest_history_id,
                    entity_type="Picture",
                    entity_id=pic.id,
                    entity_name=getattr(pic, 'file_path', None),
                    action=obj_action,
                ))

    # CastRoleMaps
    for rm in getattr(ctx, 'cast_role_maps', []):
        obj_action = await _decide_action_for_related_object(db, "CastRoleMap", rm.map_id, content_id, rm.updated_at)
        if obj_action != "SKIP":
            objects_to_mark.append({
                "entity_type": "CastRoleMap",
                "entity_id": rm.map_id,
                "action": obj_action,  # 使用判断出的 Action
            })
            if ingest_history_id:
                detail_records.append(IngestHistoryDetail(
                    history_id=ingest_history_id,
                    entity_type="CastRoleMap",
                    entity_id=rm.map_id,
                    entity_name=getattr(rm, 'role_name', None),
                    action=obj_action,
                ))

    # PhysicalChannels（Channel 类型专用）
    for pc in getattr(ctx, 'physical_channels', []):
        obj_action = await _decide_action_for_related_object(db, "PhysicalChannel", pc.id, content_id, pc.updated_at)
        if obj_action != "SKIP":
            objects_to_mark.append({
                "entity_type": "PhysicalChannel",
                "entity_id": pc.id,
                "action": obj_action,  # 使用判断出的 Action
            })
            if ingest_history_id:
                detail_records.append(IngestHistoryDetail(
                    history_id=ingest_history_id,
                    entity_type="PhysicalChannel",
                    entity_id=pc.id,
                    entity_name=getattr(pc, 'name', None),
                    action=obj_action,
                ))
        await _sync_object_ingest_status(db, "PhysicalChannel", pc.id, "success")

    # 批量更新 object_publish_status（仅非 SKIP 对象）
    await batch_mark_objects_as_published(
        db, objects_to_mark, content_id, action
    )

    # 批量写入 ingest_history_detail
    if detail_records:
        db.add_all(detail_records)
        await db.flush()

    logger.info(
        f"从BuildContext批量标记 {len(objects_to_mark)} 个对象 | "
        f"content_id={content_id} action={action} | "
        f"detail_records={len(detail_records)}"
    )


async def _decide_action_for_related_object(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_id: int,
    obj_updated_at: Optional[datetime] = None,
) -> str:
    """
    判断关联对象的Action（用于写入IngestHistoryDetail）。

    与 decide_action_for_object 逻辑保持一致：
    - 从未发布过 → REGIST
    - 已发布且有变更 → UPDATE
    - 已发布且无变更 → SKIP

    Args:
        db: 数据库会话
        entity_type: 实体类型
        entity_id: 实体ID
        content_id: 关联的主内容ID
        obj_updated_at: ORM对象的updated_at时间，用于变更检测

    Returns:
        "REGIST" / "UPDATE" / "SKIP"
    """
    history = await publish_repository.get_object_publish_status(
        db, entity_type, entity_id
    )

    if history and history.is_published:
        # 曾经发布成功过，检查是否有变更
        if (
            obj_updated_at is not None
            and history.last_publish_time is not None
            and obj_updated_at <= history.last_publish_time
        ):
            return "SKIP"
        return "UPDATE"
    return "REGIST"
