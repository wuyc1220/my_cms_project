"""
批量导入服务。

职责：
- 在单个事务中批量创建 EPISODE / SERIES 子内容
- 同时记录操作历史、自动创建安排任务
- 失败时整体回滚，保证数据一致性
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_package.models.enums import ContentStatus, ContentType
from app.internal.cms_biz_package.models.package import Content, ContentGenre
from app.internal.cms_biz_package.services import task_service
from app.internal.cms_biz_scp.models.trade import LicenseContent
from app.internal.cms_biz_orchestration.models.episode_history import EpisodeHistory
from app.internal.cms_biz_orchestration.schemas.content import BatchImportItem, BatchImportResultItem
from app.internal.cms_biz_orchestration.services import episode_history_service
from app.internal.cms_biz_orchestration.services.workflow_service import complete_process_and_update_status, rollback_after_published_edit

logger = logging.getLogger(__name__)

# 批量导入时，所有序列号计算从该值开始递增
BATCH_SERIES_ORDINAL_BASE = 1000


async def batch_create_sub_contents(
    db: AsyncSession,
    parent_id: int,
    items: list[BatchImportItem],
    current_user,
) -> tuple[list[BatchImportResultItem], str]:
    """在单个事务中批量导入子内容。

    Args:
        db: 数据库会话
        parent_id: 父级内容 ID
        items: 要创建的内容列表（已校验合法数据）
        current_user: 当前登录用户

    Returns:
        (results, parent_content_type) — 结果列表和父节点内容类型
    """
    processed_by = current_user.username
    results: list[BatchImportResultItem] = []

    # 1. 获取父节点信息
    parent = (
        await db.execute(
            select(Content).where(Content.id == parent_id, Content.is_deleted.is_(False))
        )
    ).scalar_one_or_none()

    parent_ctype = parent.content_type if parent else "UNKNOWN"

    # 获取父内容的题材 ID 列表（从 ContentGenre 中间表）
    parent_genre_ids: list[int] = []
    if parent:
        genre_rows = (await db.execute(
            select(ContentGenre.genre_id).where(
                ContentGenre.content_id == parent_id,
                ContentGenre.is_deleted.is_(False),
            )
        )).all()
        parent_genre_ids = [r[0] for r in genre_rows]

    # 2. 校验完成性：检查所有标题/序号是否在数据库中已存在
    existing_titles = set()
    existing_ordinals: set[int] = set()

    if parent_ctype == "SEASON":
        existing_rows = (
            await db.execute(
                select(Content.title, Content.series_ordinal).where(
                    Content.parent_id == parent_id,
                    Content.content_type.in_(["SERIES", "SEASON_SERIES"]),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).all()
    elif parent_ctype == "SERIES":
        existing_rows = (
            await db.execute(
                select(Content.title, Content.sequence).where(
                    Content.parent_id == parent_id,
                    Content.content_type == "EPISODE",
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
            )
        ).all()
    else:
        existing_rows = []

    for row in existing_rows:
        existing_titles.add(row[0])
        if len(row) > 1 and row[1] is not None:
            existing_ordinals.add(row[1])

    # 3. 逐条创建
    for item in items:
        ctype = item.content_type
        try:
            # 校验标题唯一性
            if item.title in existing_titles:
                raise ValueError(f"名称已存在: {item.title}")
            existing_titles.add(item.title)

            # 校验序号唯一性
            if ctype == "SERIES" and item.series_ordinal is not None:
                if item.series_ordinal in existing_ordinals:
                    raise ValueError(f"季序号 {item.series_ordinal} 已存在")
                existing_ordinals.add(item.series_ordinal)
            elif ctype == "EPISODE" and item.sequence is not None:
                if item.sequence in existing_ordinals:
                    raise ValueError(f"集序号 {item.sequence} 已存在")
                existing_ordinals.add(item.sequence)

            # 创建 Content 记录（flush 获取 id，不提交）
            series_type_val = None
            if ctype in ("SERIES", "SEASON_SERIES"):
                series_type_val = item.series_type if item.series_type else 2

            main_content = Content(
                content_type=ctype,
                title=item.title,
                status=ContentStatus.NONE.value,
                parent_id=parent_id,
                series_type=series_type_val,
                series_ordinal=item.series_ordinal if ctype in ("SERIES", "SEASON_SERIES") else None,
                sequence=item.sequence if ctype == "EPISODE" else None,
            )
            db.add(main_content)
            await db.flush()

            # 继承父内容的题材（复制到 ContentGenre 中间表）
            if parent_genre_ids:
                for gid in parent_genre_ids:
                    db.add(ContentGenre(
                        content_id=main_content.id,
                        genre_id=gid,
                        created_by=current_user.id,
                    ))

            history = EpisodeHistory(
                parent_id=parent_id,
                content_id=main_content.id,
                content_name=item.title,
                content_type=ctype,
                series_ordinal=item.series_ordinal if ctype in ("SERIES", "SEASON_SERIES") else None,
                processed_by=processed_by,
                processed_type="Add",
                created_by=current_user.id,
            )
            db.add(history)

            # 创建 arrangement 任务（与单个创建路径一致：未指定负责人时任务为 Not Assigned，进入任务池可被分配）
            await task_service.create_arrangement_task(
                db,
                main_content.id,
                assignee_id=item.assignee_id,
                processed_by=processed_by,
            )

            # ── 继承父内容许可证（与 create_content 对齐，bug 32459）──
            # 批量导入的子内容（SEASON_SERIES/SERIES/EPISODE）自动继承父内容的许可证关联
            if ctype in (ContentType.SEASON_SERIES.value, ContentType.SERIES.value, ContentType.EPISODE.value):
                parent_license_ids = (
                    await db.execute(
                        select(LicenseContent.license_id).where(
                            LicenseContent.content_id == parent_id,
                            LicenseContent.is_deleted.is_(False),
                        )
                    )
                ).scalars().all()
                if parent_license_ids:
                    existing_license_ids = set(
                        (
                            await db.execute(
                                select(LicenseContent.license_id).where(
                                    LicenseContent.content_id == main_content.id,
                                )
                            )
                        ).scalars().all()
                    )
                    added_license_ids: list[int] = []
                    for lid in parent_license_ids:
                        if lid in existing_license_ids:
                            continue
                        db.add(LicenseContent(license_id=lid, content_id=main_content.id))
                        added_license_ids.append(lid)
                    if added_license_ids:
                        logger.info(
                            f"批量导入-继承父内容 #{parent_id} 许可证: {added_license_ids} → 子内容 #{main_content.id}"
                        )

            results.append(
                BatchImportResultItem(
                    title=item.title,
                    content_id=main_content.id,
                    success=True,
                )
            )

            logger.info(
                f"批量导入-创建成功: type={ctype}, title={item.title}, "
                f"id={main_content.id}, parent_id={parent_id}"
            )

        except Exception as e:
            logger.warning(
                f"批量导入-创建失败: type={ctype}, title={item.title}, error={e}"
            )
            results.append(
                BatchImportResultItem(
                    title=item.title,
                    success=False,
                    error=str(e),
                )
            )

    # 4. 记录父节点 InjectSubContent 流程状态（如果有成功创建的）
    success_items = [r for r in results if r.success]
    if success_items:
        try:
            # 新增子内容后递归回退所有祖先状态（与单个创建 API 行为一致）
            from app.internal.cms_biz_orchestration.services.workflow_service import rollback_ancestors_after_child_change
            await rollback_ancestors_after_child_change(
                db,
                start_parent_id=parent_id,
                edited_by=processed_by,
                edit_info=f"批量导入 {len(success_items)} 个子内容",
            )
            # 再记录 InjectSubContent 流程节点完成（Processed By 显示当前操作人）
            await complete_process_and_update_status(
                db,
                content_id=parent_id,
                content_type=parent_ctype,
                process_name="InjectSubContent",
                processed_by=processed_by,
                info=f"批量导入 {len(success_items)} 个子内容",
            )
        except Exception as e:
            logger.warning(f"批量导入-流程状态记录失败: {e}")

    return results, parent_ctype
