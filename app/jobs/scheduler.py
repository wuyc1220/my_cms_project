"""
内置定时任务调度器（基于 APScheduler）。

设计目标：
- 使用 APScheduler 的 `AsyncIOScheduler` + `CronTrigger` 执行定时任务，
  原生支持 cron 表达式与异步协程作业。
- 启动时从 `scheduled_task` 表加载所有 `schedule_status=enabled` 的任务，
  根据 `task_type` 在 `_TASK_REGISTRY` 中查找对应协程并注册到调度器。
- 每次执行（无论 scheduled 还是 manual）均走统一包装器：
    * 执行前写一条 running 日志、更新 task.execution_status=running
    * 执行后回写 success/failed 日志、task.execution_status=idle、
      以及 last_execution_time / next_execution_time。

cron 表达式格式（兼容 5 位标准与 6 位含秒扩展）：
    5 位：分 时 日 月 星期          例：`0 0 * * *`   → 每日 00:00
    6 位：秒 分 时 日 月 星期       例：`1 0 0 * * *` → 每日 00:00:01
"""

import time
from datetime import datetime
from typing import Awaitable, Callable

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings, app_tz
from app.database import AsyncSessionLocal
from app.internal.cms_biz_system.models.scheduled_task import (
    ScheduledTask,
    ScheduledTaskLog,
)
from app.jobs.content_offline_service import scan_and_offline_expired_contents
from app.jobs.content_publish_service import run_content_publish
from app.jobs.content_scheduled_offline_service import run_content_scheduled_offline
from app.jobs.content_archive_service import run_content_archive
from app.jobs.metadata_quality_check_service import run_metadata_quality_check


# 每个定时任务的业务执行协程签名：接收 DB Session，返回执行摘要 dict
TaskFunc = Callable[[AsyncSession], Awaitable[dict]]


async def _task_metadata_quality_check(db: AsyncSession, check_id: int | None = None) -> dict:
    """包装 MetadataQualityCheck：定时触发时 operator=None。"""
    return await run_metadata_quality_check(db, operator=None, check_id=check_id)


# task_type → 业务协程
_TASK_REGISTRY: dict[str, TaskFunc] = {
    "ContentOffline": scan_and_offline_expired_contents,
    "ContentScheduledOffline": run_content_scheduled_offline,
    "ContentPublish": run_content_publish,
    "ContentArchive": run_content_archive,
    "MetadataQualityCheck": _task_metadata_quality_check,
}

_LAZY_LOG_TASKS: set[str] = {"ContentPublish", "ContentScheduledOffline", "ContentArchive"}


_SCHEDULER_TZ = app_tz


def _build_cron_trigger(cron_expr: str) -> CronTrigger:
    """
    将 5/6 位 cron 表达式转换为 APScheduler 的 CronTrigger。

    - 5 位：分 时 日 月 星期     → 使用 `CronTrigger.from_crontab`
    - 6 位：秒 分 时 日 月 星期  → 手动拆字段构造 CronTrigger

    时区由 APP_TIMEZONE 配置决定，默认 Asia/Shanghai。
    """
    fields = cron_expr.split()
    if len(fields) == 5:
        return CronTrigger.from_crontab(cron_expr, timezone=_SCHEDULER_TZ)
    if len(fields) == 6:
        second, minute, hour, day, month, day_of_week = fields
        return CronTrigger(
            second=second,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=_SCHEDULER_TZ,
        )
    raise ValueError(f"无效的 cron 表达式（仅支持 5 或 6 位）：{cron_expr}")


def _next_fire_time(cron_expr: str) -> datetime | None:
    """基于 cron 推导下一次触发时间，失败返回 None。"""
    try:
        trigger = _build_cron_trigger(cron_expr)
    except ValueError:
        return None
    return trigger.get_next_fire_time(None, datetime.now(_SCHEDULER_TZ))


def _get_job_next_run_time(task_type: str) -> datetime | None:
    """
    从 APScheduler 作业对象中获取真实的下次触发时间。
    
    这比重新计算 cron 更准确，因为：
    1. 考虑了调度器的实际状态
    2. 考虑了 misfire、coalesce 等配置
    3. 避免了时区和计算误差
    """
    if _scheduler is None or not _scheduler.running:
        # 调度器未运行，返回 None
        return None
    
    job = _scheduler.get_job(task_type)
    if job is not None and job.next_run_time is not None:
        return job.next_run_time
    
    # 如果作业不存在（可能在执行中被移除），返回 None
    return None


# 运行时持有的 APScheduler 实例（单例）
_scheduler: AsyncIOScheduler | None = None


def _on_job_event(event: JobExecutionEvent) -> None:
    """APScheduler 作业事件回调：统一记录成功/异常日志。"""
    if event.exception is not None:
        logger.exception(f"[Scheduler] 任务 {event.job_id} 执行异常：{event.exception}")
    else:
        logger.info(f"[Scheduler] 任务 {event.job_id} 执行完成")


async def _execute_batch(func: TaskFunc, task_type: str, check_id: int | None = None) -> dict | None:
    """
    手动触发时的批量执行：循环调用业务函数直到无更多任务。

    每轮使用独立 DB Session，汇总所有轮次的执行结果。
    无待处理任务时返回 None。

    对于 MetadataQualityCheck 等非批量扫描任务，直接透传业务函数的返回值。

    Args:
        func: 业务函数
        task_type: 任务类型
        check_id: 可选，元数据质检记录ID。仅用于 MetadataQualityCheck 任务
    """
    if task_type == "MetadataQualityCheck":
        async with AsyncSessionLocal() as db:
            if check_id is not None:
                summary = await func(db, check_id=check_id)
            else:
                summary = await func(db)
        return summary

    total_triggered = 0
    total_succeeded = 0
    total_failed = 0
    total_exhausted = 0
    max_retry_val = 0
    last_summary = None

    while True:
        async with AsyncSessionLocal() as db:
            summary = await func(db)

        if summary is None:
            break
        
        last_summary = summary

        if isinstance(summary, dict):
            total_triggered += summary.get("triggered", 0)
            total_succeeded += summary.get("succeeded", 0)
            total_failed += summary.get("failed", 0)
            total_exhausted += summary.get("exhausted", 0)
            max_retry_val = summary.get("max_retry", 0)

        if not isinstance(summary, dict) or not summary.get("has_more", False):
            break

    if total_triggered == 0:
        return None

    result = {
        "triggered": total_triggered,
        "succeeded": total_succeeded,
        "failed": total_failed,
        "exhausted": total_exhausted,
        "max_retry": max_retry_val,
        "has_more": False,
    }
    
    if last_summary and isinstance(last_summary, dict):
        for key, value in last_summary.items():
            if key not in result:
                result[key] = value
    
    return result


async def _execute_task(task_type: str, trigger_type: str, operator: str | None = None, check_id: int | None = None) -> None:
    """
    任务统一执行包装器（独立 DB Session）。

    行为：
    1. 查询 scheduled_task 记录；无则记录日志并返回。
    2. 写 running 日志 + 更新 task.execution_status=running。
    3. 调用 _TASK_REGISTRY 中的业务协程。
    4. 成功/失败回写 log 与 task。

    对于 _LAZY_LOG_TASKS 中的高频扫描任务：
    不预先写 running 日志，仅标记 execution_status=running；
    执行后若无业务数据则不产生任何日志记录，有数据才写入最终日志。

    Args:
        task_type: 任务类型
        trigger_type: 触发类型（'scheduled' 或 'manual'）
        operator: 操作人（手动触发时记录）
        check_id: 可选，元数据质检记录ID。仅用于 MetadataQualityCheck 任务
    """
    func = _TASK_REGISTRY.get(task_type)
    if func is None:
        logger.error(f"[Scheduler] 未注册的任务类型：{task_type}")
        return

    lazy_log = task_type in _LAZY_LOG_TASKS
    start_ts = time.monotonic()
    start_dt = datetime.now(_SCHEDULER_TZ)

    # 1) 查询任务配置 + 标记运行状态
    task_id: int | None = None
    cron_expr: str | None = None
    running_log_id: int | None = None
    async with AsyncSessionLocal() as db:
        task = (await db.execute(
            select(ScheduledTask).where(ScheduledTask.task_type == task_type)
        )).scalar_one_or_none()
        if task is None:
            logger.error(f"[Scheduler] 数据库中未找到任务：{task_type}")
            return

        task_id = task.id
        cron_expr = task.cron_expression

        if lazy_log:
            task.execution_status = "running"
            await db.commit()
        else:
            running_log = ScheduledTaskLog(
                task_id=task.id,
                execution_time=start_dt,
                trigger_type=trigger_type,
                execution_status="running",
                duration=None,
                result=None,
            )
            db.add(running_log)
            task.execution_status = "running"
            await db.commit()
            await db.refresh(running_log)
            running_log_id = running_log.id

    # 2) 执行业务
    status = "success"
    result_summary: str | None = None
    has_more = False
    try:
        if trigger_type == "manual":
            summary = await _execute_batch(func, task_type, check_id=check_id)
        else:
            async with AsyncSessionLocal() as db:
                # MetadataQualityCheck 任务支持 check_id 参数
                if task_type == "MetadataQualityCheck" and check_id is not None:
                    summary = await func(db, check_id=check_id)
                else:
                    summary = await func(db)

        if summary is None:
            # 无待处理任务，但仍然需要更新执行时间
            next_fire = _get_job_next_run_time(task_type)
            async with AsyncSessionLocal() as db:
                if not lazy_log and running_log_id is not None:
                    log = await db.get(ScheduledTaskLog, running_log_id)
                    if log is not None:
                        await db.delete(log)
                task_row = await db.get(ScheduledTask, task_id)
                if task_row is not None:
                    task_row.execution_status = "idle"
                    task_row.last_execution_time = start_dt
                    task_row.next_execution_time = next_fire
                await db.commit()
            if next_fire is not None:
                logger.info(f"[Scheduler] 任务 {task_type}({trigger_type}) 无待处理项，下次执行时间: {next_fire}")
            else:
                logger.info(f"[Scheduler] 任务 {task_type}({trigger_type}) 无待处理项")
            return

        result_summary = str(summary)
        has_more = isinstance(summary, dict) and summary.get("has_more", False)
        logger.info(f"[Scheduler] 任务 {task_type}({trigger_type}) 执行完成：{result_summary}")
    except Exception as exc:  # noqa: BLE001
        status = "failed"
        result_summary = f"{type(exc).__name__}: {exc}"
        logger.exception(f"[Scheduler] 任务 {task_type}({trigger_type}) 执行异常")

    # 3) 回写日志与任务状态（独立短事务）
    duration = round(time.monotonic() - start_ts, 2)
    
    # 从 APScheduler 获取真实的下次触发时间，而不是重新计算
    next_fire = _get_job_next_run_time(task_type)
    if next_fire is not None:
        logger.info(f"[Scheduler] 任务 {task_type} 下次执行时间: {next_fire}")
    else:
        logger.warning(f"[Scheduler] 任务 {task_type} 无法获取下次执行时间")

    async with AsyncSessionLocal() as db:
        if lazy_log:
            log = ScheduledTaskLog(
                task_id=task_id,
                execution_time=start_dt,
                trigger_type=trigger_type,
                execution_status=status,
                duration=duration,
                result=(result_summary or "")[:2000],
            )
            db.add(log)
        else:
            log = await db.get(ScheduledTaskLog, running_log_id)
            if log is not None:
                log.execution_status = status
                log.duration = duration
                log.result = (result_summary or "")[:2000]

        task_row = await db.get(ScheduledTask, task_id)
        if task_row is not None:
            task_row.execution_status = "idle"
            task_row.last_execution_time = start_dt
            task_row.next_execution_time = next_fire

        await db.commit()

    # 4) 若仍有待处理任务，立即调度下一轮
    if has_more and _scheduler is not None and _scheduler.running:
        _scheduler.add_job(
            _scheduled_wrapper,
            trigger=DateTrigger(run_date=datetime.now(_SCHEDULER_TZ)),
            args=[task_type],
            id=f"continue-{task_type}-{int(time.time() * 1000)}",
            name=f"continue-{task_type}",
            replace_existing=False,
            max_instances=1,
        )
        logger.info(f"[Scheduler] 任务 {task_type} 仍有待处理项，已调度立即执行")


async def _scheduled_wrapper(task_type: str) -> None:
    """APScheduler 定时调度入口：固定 trigger_type='scheduled'。"""
    await _execute_task(task_type, trigger_type="scheduled", operator=None)


async def trigger_task_manual(db: AsyncSession, task_type: str, operator: str | None = None, check_id: int | None = None) -> None:
    """
    API 手动触发入口。

    - 校验任务存在、schedule_status=enabled、execution_status=idle；
    - 将 `_execute_task(..., trigger_type='manual')` 通过 APScheduler 立即调度一次，
      调度器未启用时则直接以 asyncio 任务形式执行。

    Args:
        db: 数据库会话
        task_type: 任务类型
        operator: 操作人（手动触发时记录）
        check_id: 可选，元数据质检记录ID。仅用于 MetadataQualityCheck 任务
    """
    task = (await db.execute(
        select(ScheduledTask).where(ScheduledTask.task_type == task_type)
    )).scalar_one_or_none()
    if task is None:
        raise ValueError(f"任务不存在：{task_type}")
    if task.schedule_status != "enabled":
        raise ValueError("任务未启用，无法手动触发")
    if task.execution_status == "running":
        raise ValueError("任务正在执行中，请稍后再试")

    import asyncio

    if _scheduler is not None and _scheduler.running:
        _scheduler.add_job(
            _execute_task,
            args=[task_type, "manual", operator, check_id],
            id=f"manual-{task_type}-{int(time.time() * 1000)}",
            name=f"manual-{task_type}",
            replace_existing=False,
            misfire_grace_time=60,
        )
    else:
        asyncio.create_task(_execute_task(task_type, "manual", operator, check_id))


async def reload_task(task_type: str) -> None:
    """
    按 task_type 从 DB 重新加载/禁用任务调度。
    enabled → 重新注册；disabled → 移除。
    """
    if _scheduler is None or not _scheduler.running:
        return

    async with AsyncSessionLocal() as db:
        task = (await db.execute(
            select(ScheduledTask).where(ScheduledTask.task_type == task_type)
        )).scalar_one_or_none()

    # 先移除旧的（忽略不存在）
    try:
        _scheduler.remove_job(task_type)
    except Exception:  # noqa: BLE001
        pass

    if task is None or task.schedule_status != "enabled":
        logger.info(f"[Scheduler] 任务 {task_type} 已从调度器移除")
        return

    try:
        trigger = _build_cron_trigger(task.cron_expression)
    except ValueError as exc:
        logger.error(f"[Scheduler] 任务 {task_type} 重新加载失败：{exc}")
        return

    _scheduler.add_job(
        _scheduled_wrapper,
        trigger=trigger,
        args=[task_type],
        id=task_type,
        name=task_type,
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )
    logger.info(f"[Scheduler] 任务 {task_type} 已重新加载，cron={task.cron_expression}")


async def start_scheduler() -> None:
    """启动调度器并从 DB 注册所有启用状态的任务。"""
    global _scheduler

    if not settings.scheduler_enabled:
        logger.info("内置定时调度器未启用（SCHEDULER_ENABLED=false），跳过启动")
        return

    if _scheduler is not None and _scheduler.running:
        logger.warning("内置定时调度器已启动，忽略重复 start")
        return

    scheduler = AsyncIOScheduler(timezone=_SCHEDULER_TZ)
    scheduler.add_listener(_on_job_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

    async with AsyncSessionLocal() as db:
        tasks = (await db.execute(
            select(ScheduledTask).where(ScheduledTask.schedule_status == "enabled")
        )).scalars().all()

    for task in tasks:
        if task.task_type not in _TASK_REGISTRY:
            logger.warning(f"[Scheduler] 任务 {task.task_type} 未在 _TASK_REGISTRY 中注册，跳过")
            continue
        try:
            trigger = _build_cron_trigger(task.cron_expression)
        except ValueError as exc:
            logger.error(f"[Scheduler] 任务 {task.task_type} 注册失败：{exc}")
            continue

        scheduler.add_job(
            _scheduled_wrapper,
            trigger=trigger,
            args=[task.task_type],
            id=task.task_type,
            name=task.task_type,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        logger.info(f"[Scheduler] 任务 {task.task_type} 已登记，cron={task.cron_expression}")

    scheduler.start()
    _scheduler = scheduler

    # 启动后回写每个任务的 next_execution_time（便于前端立即展示）
    await _sync_next_execution_times()

    logger.info(f"内置定时调度器已启动，共 {len(scheduler.get_jobs())} 个任务")


async def _sync_next_execution_times() -> None:
    """根据当前已注册的作业，把 next_run_time 写回 scheduled_task 表。"""
    if _scheduler is None:
        return
    async with AsyncSessionLocal() as db:
        for job in _scheduler.get_jobs():
            task = (await db.execute(
                select(ScheduledTask).where(ScheduledTask.task_type == job.id)
            )).scalar_one_or_none()
            if task is not None and job.next_run_time is not None:
                task.next_execution_time = job.next_run_time
        await db.commit()


async def stop_scheduler() -> None:
    """关闭 APScheduler 调度器并等待运行中作业退出。"""
    global _scheduler

    if _scheduler is None or not _scheduler.running:
        return

    logger.info(f"内置定时调度器正在关闭，当前任务数 {len(_scheduler.get_jobs())}")
    _scheduler.shutdown(wait=True)
    _scheduler = None
    logger.info("内置定时调度器已停止")
