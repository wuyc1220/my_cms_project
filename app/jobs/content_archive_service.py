"""
ContentArchive 定时任务业务逻辑。

扫描 content 表中到期的计划归档节目单并逐条执行归档。

一次查出所有到期任务，循环中逐条处理（每条独立 commit），单条失败不影响其他。
无待执行任务时返回 None，调度器将清除本次运行日志，避免产生无效记录。

触发条件：
  content_type = 'SCHEDULE'
  is_archived = false
  archive_scheduled_time IS NOT NULL
  archive_scheduled_time <= NOW()
  is_deleted = false
  cutv_enable = true
"""
from datetime import datetime

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import app_tz
from app.internal.cms_biz_orchestration.schemas.live import ArchiveRequest
from app.internal.cms_biz_orchestration.services import live_service
from app.internal.cms_biz_package.models.package import Content


async def run_content_archive(db: AsyncSession) -> dict | None:
    """
    扫描到期的计划归档节目单并逐条执行。

    一次查出所有到期任务，循环中逐条处理，每条独立 commit，单条失败不影响其他。
    无待执行任务时返回 None，调度器据此清除运行日志。
    """
    logger.info(f"[ContentArchive] ========== 任务开始 ==========")
    now = datetime.now(app_tz)
    logger.info(f"[ContentArchive] 当前时间={now}")
    logger.info(f"[ContentArchive] 扫描条件: content_type='SCHEDULE', is_archived=False, archive_scheduled_time<={now}, cutv_enable=True")

    query = (
        select(Content)
        .where(
            Content.content_type == "SCHEDULE",
            Content.is_archived.is_(False),
            Content.archive_scheduled_time.isnot(None),
            Content.archive_scheduled_time <= now,
            Content.is_deleted.is_(False),
            Content.cutv_enable.is_(True),
        )
        .order_by(Content.archive_scheduled_time.asc())
    )
    rows = (await db.execute(query)).scalars().all()
    logger.info(f"[ContentArchive] 扫描完成，共找到 {len(rows)} 条待处理归档任务")

    if not rows:
        logger.info(f"[ContentArchive] 无待处理任务，任务结束")
        logger.info(f"[ContentArchive] ========== 任务结束 ==========")
        return None

    succeeded = 0
    failed = 0

    for idx, schedule in enumerate(rows, 1):
        schedule_id = schedule.id
        logger.info(f"[ContentArchive] [{idx}/{len(rows)}] 处理节目单 ID={schedule_id}, title={schedule.title}, archive_time={schedule.archive_scheduled_time}")

        try:
            logger.info(f"[ContentArchive] [{idx}/{len(rows)}] 开始执行节目单归档 ID={schedule_id}, mode=now")
            result = await live_service.archive_schedule(
                db,
                ArchiveRequest(schedule_id=schedule_id, mode="now"),
            )
            await db.commit()
            if result.success:
                succeeded += 1
                logger.info(f"[ContentArchive] [{idx}/{len(rows)}] 节目单 {schedule_id} 计划归档执行成功")
            else:
                failed += 1
                logger.error(f"[ContentArchive] [{idx}/{len(rows)}] 节目单 {schedule_id} 计划归档返回失败: {result.message}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            logger.exception(f"[ContentArchive] [{idx}/{len(rows)}] 节目单 {schedule_id} 计划归档执行异常：{exc}")
            try:
                schedule.archive_scheduled_time = None
                await db.commit()
                logger.info(f"[ContentArchive] [{idx}/{len(rows)}] 节目单 {schedule_id} archive_scheduled_time 已清空")
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.error(f"[ContentArchive] [{idx}/{len(rows)}] 节目单 {schedule_id} 异常回滚失败")

    summary = {
        "triggered": len(rows),
        "succeeded": succeeded,
        "failed": failed,
    }
    logger.info(f"[ContentArchive] 任务执行完成：triggered={len(rows)}, succeeded={succeeded}, failed={failed}")
    logger.info(f"[ContentArchive] ========== 任务结束 ==========")
    return summary
