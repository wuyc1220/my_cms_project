"""
对象发布状态管理服务。

统一 REGIST 策略后，不再需要 Action 判定逻辑。
保留：
  - 关联对象 ingest_status 同步
  - IngestHistoryDetail 明细写入
  - object_publish_status 表更新（记录发布时间，用于审计）
"""
from typing import Optional

from loguru import logger
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_publish.repositories import publish_repository
from app.database import Base


_ENTITY_TYPE_INGEST_TABLE_MAP: dict[str, str] = {
    "Cast": "cast",
    "Category": "category",
    "Package": "package",
    "Movie": "movie",
    "PhysicalChannel": "physical_channel",
    "Picture": "picture",
}


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
        table = Base.metadata.tables.get(f"{Base.metadata.schema}.{table_name}")
    if table is None:
        return
    await db.execute(
        sa_update(table)
        .where(table.c.id == entity_id)
        .values(
            ingest_status=ingest_status,
            # 保持 updated_at 不变：ingest_status 是发布流程回写的同步状态而非业务变更
            updated_at=table.c.updated_at,
        )
    )


async def batch_mark_objects_from_context(
    db: AsyncSession,
    content_id: int,
    ctx,
    action: str = "REGIST",
    ingest_history_id: Optional[int] = None,
) -> None:
    """
    从 BuildContext 批量标记所有涉及的对象为发布成功。

    统一 REGIST 策略后，所有对象 action 均为 REGIST。
    同时完成：
    1. 更新 object_publish_status 表
    2. 同步关联对象的 ingest_status 字段
    3. 写入 ingest_history_detail 明细记录
    """
    from app.internal.cms_biz_orchestration.models.ingest_history_detail import IngestHistoryDetail

    objects_to_mark = []
    detail_records = []

    objects_to_mark.append({
        "entity_type": "Content",
        "entity_id": content_id,
        "action": "REGIST",
    })

    for movie in getattr(ctx, 'movies', []):
        objects_to_mark.append({
            "entity_type": "Movie",
            "entity_id": movie.id,
            "action": "REGIST",
        })
        if ingest_history_id:
            movie_name = getattr(movie, 'file_name', None) or getattr(movie, 'file_path', None)
            if movie_name and len(movie_name) > 50:
                movie_name = movie_name[:50]
            detail_records.append(IngestHistoryDetail(
                history_id=ingest_history_id,
                entity_type="Movie",
                entity_id=movie.id,
                entity_name=movie_name,
                action="REGIST",
            ))
        await _sync_object_ingest_status(db, "Movie", movie.id, "success")

    for cast in getattr(ctx, 'casts', []):
        objects_to_mark.append({
            "entity_type": "Cast",
            "entity_id": cast.id,
            "action": "REGIST",
        })
        if ingest_history_id:
            cast_name = getattr(cast, 'name', None)
            if cast_name and len(cast_name) > 50:
                cast_name = cast_name[:50]
            detail_records.append(IngestHistoryDetail(
                history_id=ingest_history_id,
                entity_type="Cast",
                entity_id=cast.id,
                entity_name=cast_name,
                action="REGIST",
            ))
        await _sync_object_ingest_status(db, "Cast", cast.id, "success")

    for cat, _ in getattr(ctx, 'categories', []):  # categories is list[tuple[Category, int]]
        # 与 Package 行为对齐：每次发布都写明细并刷新发布状态（无 SKIP 去重）
        objects_to_mark.append({
            "entity_type": "Category",
            "entity_id": cat.id,
            "action": "REGIST",
        })
        if ingest_history_id:
            cat_name = getattr(cat, 'name', None)
            if cat_name and len(cat_name) > 50:
                cat_name = cat_name[:50]
            detail_records.append(IngestHistoryDetail(
                history_id=ingest_history_id,
                entity_type="Category",
                entity_id=cat.id,
                entity_name=cat_name,
                action="REGIST",
            ))
        await _sync_object_ingest_status(db, "Category", cat.id, "success")

    for pkg in getattr(ctx, 'packages', []):
        objects_to_mark.append({
            "entity_type": "Package",
            "entity_id": pkg.id,
            "action": "REGIST",
        })
        if ingest_history_id:
            pkg_name = getattr(pkg, 'name', None)
            if pkg_name and len(pkg_name) > 50:
                pkg_name = pkg_name[:50]
            detail_records.append(IngestHistoryDetail(
                history_id=ingest_history_id,
                entity_type="Package",
                entity_id=pkg.id,
                entity_name=pkg_name,
                action="REGIST",
            ))
        await _sync_object_ingest_status(db, "Package", pkg.id, "success")

    for pic in getattr(ctx, 'pictures', []):
        objects_to_mark.append({
            "entity_type": "Picture",
            "entity_id": pic.id,
            "action": "REGIST",
        })
        await _sync_object_ingest_status(db, "Picture", pic.id, "success")
        if ingest_history_id:
            pic_name = getattr(pic, 'file_name', None) or getattr(pic, 'file_path', None)
            if pic_name and len(pic_name) > 50:
                pic_name = pic_name[:50]
            detail_records.append(IngestHistoryDetail(
                history_id=ingest_history_id,
                entity_type="Picture",
                entity_id=pic.id,
                entity_name=pic_name,
                action="REGIST",
            ))

    for rm in getattr(ctx, 'cast_role_maps', []):
        objects_to_mark.append({
            "entity_type": "CastRoleMap",
            "entity_id": rm.map_id,
            "action": "REGIST",
        })
        if ingest_history_id:
            role_name = getattr(rm, 'role_name', None)
            if role_name and len(role_name) > 50:
                role_name = role_name[:50]
            detail_records.append(IngestHistoryDetail(
                history_id=ingest_history_id,
                entity_type="CastRoleMap",
                entity_id=rm.map_id,
                entity_name=role_name,
                action="REGIST",
            ))

    for pc in getattr(ctx, 'physical_channels', []):
        objects_to_mark.append({
            "entity_type": "PhysicalChannel",
            "entity_id": pc.id,
            "action": "REGIST",
        })
        if ingest_history_id:
            pc_name = getattr(pc, 'name', None)
            if pc_name and len(pc_name) > 50:
                pc_name = pc_name[:50]
            detail_records.append(IngestHistoryDetail(
                history_id=ingest_history_id,
                entity_type="PhysicalChannel",
                entity_id=pc.id,
                entity_name=pc_name,
                action="REGIST",
            ))
        await _sync_object_ingest_status(db, "PhysicalChannel", pc.id, "success")

    for obj in objects_to_mark:
        status = await publish_repository.get_or_create_object_publish_status(
            db, obj["entity_type"], obj["entity_id"], content_id
        )
        status.mark_as_published(obj["action"])
        await publish_repository.update_object_publish_status(db, status)

    if detail_records:
        db.add_all(detail_records)
        await db.flush()

    logger.info(
        f"从 BuildContext 批量标记 {len(objects_to_mark)} 个对象 | "
        f"content_id={content_id} | detail_records={len(detail_records)}"
    )
