"""
ContentScheduledOffline 定时任务业务逻辑。

扫描 publish_task 中到期的定时下架计划并逐条执行。

一次查出所有到期任务，循环中逐条处理（每条独立 commit），单条失败不影响其他。
无待执行任务时返回 None，调度器将清除本次运行日志，避免产生无效记录。

触发条件：
  task_type = 'unpublish'
  execution_mode = 'plan'
  scheduled_time IS NOT NULL
  scheduled_time <= NOW()
  is_deleted = false
  (
      status = 'pending'
      OR (status = 'failure' AND COALESCE(retry_attempts, 0) < max_retry)
  )
"""
from datetime import datetime

from loguru import logger
from sqlalchemy import and_, func as sa_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_publish.services import publish_service
from app.internal.cms_biz_system.models.scheduled_task import ScheduledTask


async def _get_max_retry(db: AsyncSession) -> int:
    """读取 ContentScheduledOffline 任务配置的最大重试次数。读不到时默认 0（不重试）。"""
    row = (
        await db.execute(
            select(ScheduledTask.retry_count).where(
                ScheduledTask.task_type == "ContentScheduledOffline",
                ScheduledTask.is_deleted.is_(False),
            )
        )
    ).first()
    return int(row[0]) if row and row[0] is not None else 0


async def run_content_scheduled_offline(db: AsyncSession) -> dict | None:
    """
    扫描到期的定时下架计划并逐条执行。

    一次查出所有到期任务，循环中逐条处理，每条独立 commit，单条失败不影响其他。
    无待执行任务时返回 None，调度器据此清除运行日志。
    """
    logger.info(f"[ContentScheduledOffline] ========== 任务开始 ==========")
    max_retry = await _get_max_retry(db)
    now = datetime.now()
    logger.info(f"[ContentScheduledOffline] 最大重试次数={max_retry}, 当前时间={now}")
    logger.info(f"[ContentScheduledOffline] 扫描条件: task_type='unpublish', execution_mode='plan', scheduled_time<={now}")

    query = (
        select(PublishTask)
        .where(
            PublishTask.is_deleted.is_(False),
            PublishTask.task_type == "unpublish",
            PublishTask.execution_mode == "plan",
            PublishTask.scheduled_time.isnot(None),
            PublishTask.scheduled_time <= now,
            or_(
                PublishTask.status == "pending",
                and_(
                    PublishTask.status == "failure",
                    sa_func.coalesce(PublishTask.retry_attempts, 0) < max_retry,
                ),
            ),
        )
        .order_by(PublishTask.scheduled_time.asc())
    )
    rows = (await db.execute(query)).scalars().all()
    logger.info(f"[ContentScheduledOffline] 扫描完成，共找到 {len(rows)} 条待处理下架任务")

    if not rows:
        logger.info(f"[ContentScheduledOffline] 无待处理任务，任务结束")
        logger.info(f"[ContentScheduledOffline] ========== 任务结束 ==========")
        return None

    succeeded = 0
    failed = 0
    exhausted = 0

    for idx, task in enumerate(rows, 1):
        task_id = task.id
        logger.info(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 处理任务 ID={task_id}, task_type={task.task_type}")
        logger.info(f"[ContentScheduledOffline] 任务状态={task.status}, 重试次数={task.retry_attempts}/{max_retry}")

        try:
            logger.info(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 开始执行下架任务 ID={task_id}")
            await publish_service._execute_publish_task(db, task)
            await db.commit()
            refreshed = await db.get(PublishTask, task_id)
            if refreshed is None:
                failed += 1
                logger.error(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 ID={task_id} 执行后无法获取刷新状态")
            elif refreshed.status == "success":
                succeeded += 1
                logger.info(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 ID={task_id} 执行成功")
            elif refreshed.status == "failure":
                failed += 1
                logger.error(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 ID={task_id} 执行失败，错误信息={refreshed.error_message}")
                if (refreshed.retry_attempts or 0) >= max_retry:
                    exhausted += 1
                    logger.warning(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 ID={task_id} 已达到最大重试次数 {max_retry}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 {task_id} 执行异常：{exc}")
            try:
                fresh = await db.get(PublishTask, task_id)
                if fresh is not None:
                    fresh.status = "failure"
                    fresh.error_message = f"{type(exc).__name__}: {exc}"[:2000]
                    fresh.retry_attempts = (fresh.retry_attempts or 0) + 1
                    await db.commit()
                    logger.error(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 ID={task_id} 异常已记录，重试次数={fresh.retry_attempts}/{max_retry}")
                    if (fresh.retry_attempts or 0) >= max_retry:
                        exhausted += 1
                        logger.warning(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 ID={task_id} 已达到最大重试次数 {max_retry}")
            except Exception:  # noqa: BLE001
                await db.rollback()
                logger.error(f"[ContentScheduledOffline] [{idx}/{len(rows)}] 任务 ID={task_id} 异常回滚失败")
            failed += 1

    summary = {
        "triggered": len(rows),
        "succeeded": succeeded,
        "failed": failed,
        "exhausted": exhausted,
        "max_retry": max_retry,
    }
    logger.info(f"[ContentScheduledOffline] 任务执行完成：triggered={len(rows)}, succeeded={succeeded}, failed={failed}, exhausted={exhausted}")
    logger.info(f"[ContentScheduledOffline] ========== 任务结束 ==========")
    return summary
