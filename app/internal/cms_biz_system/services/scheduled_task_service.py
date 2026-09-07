"""
定时任务（ScheduledTask）服务层。

职责：
- 列表分页查询（只返回 _TASK_REGISTRY 里已登记的任务类型，避免残留）；
- 详情查询（含最近执行日志）；
- 批量手动触发：校验 schedule/execution 状态后委托给 scheduler.trigger_task_manual。
"""

from loguru import logger
from app.common.core.i18n import get_msg
from app.common.core.exceptions import ErrorCode, BusinessException
from sqlalchemy import asc, desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.models.scheduled_task import (
    ScheduledTask,
    ScheduledTaskLog,
)
from app.internal.cms_biz_system.schemas.scheduled_task import (
    ScheduledTaskDetail,
    ScheduledTaskLogOut,
    ScheduledTaskOut,
)

# 列表默认排序字段白名单
_SORT_FIELDS = {
    "id", "task_type", "schedule_status", "execution_status",
    "last_execution_time", "next_execution_time", "created_at", "updated_at",
}


async def list_scheduled_tasks(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[ScheduledTaskOut]:
    query = select(ScheduledTask).where(ScheduledTask.is_deleted.is_(False))

    if sort_by and sort_by in _SORT_FIELDS:
        col = getattr(ScheduledTask, sort_by)
        query = query.order_by(asc(col) if sort_order == "asc" else desc(col), ScheduledTask.id.desc())
    else:
        query = query.order_by(ScheduledTask.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[ScheduledTaskOut.model_validate(r) for r in rows],
    )


async def list_scheduled_task_logs(
    db: AsyncSession,
    task_id: int,
    page: int = 1,
    page_size: int = 10,
) -> PaginatedResponse[ScheduledTaskLogOut]:
    query = (
        select(ScheduledTaskLog)
        .where(ScheduledTaskLog.task_id == task_id)
        .order_by(ScheduledTaskLog.execution_time.desc(), ScheduledTaskLog.id.desc())
    )
    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    log_outs: list[ScheduledTaskLogOut] = []
    for log in rows:
        try:
            log_outs.append(ScheduledTaskLogOut.model_validate(log))
        except Exception as exc:
            logger.exception(f"[ScheduledTaskLog] 日志序列化失败 task_id={task_id}, log_id={log.id}")
            log_outs.append(
                ScheduledTaskLogOut(
                    id=log.id,
                    execution_time=log.execution_time,
                    trigger_type=getattr(log, "trigger_type", "scheduled") or "scheduled",
                    execution_status="failed",
                    duration=None,
                    result=f"[serialize error] {type(exc).__name__}: {exc}",
                )
            )
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=log_outs,
    )


async def get_scheduled_task_detail(db: AsyncSession, task_id: int) -> ScheduledTaskDetail:
    task = (
        await db.execute(select(ScheduledTask).where(
            ScheduledTask.id == task_id,
            ScheduledTask.is_deleted.is_(False)
        ))
    ).scalar_one_or_none()
    if task is None:
        raise BusinessException(ErrorCode.SCHEDULED_TASK_NOT_FOUND, get_msg("SCHEDULED_TASK_NOT_FOUND"))

    logs = (
        await db.execute(
            select(ScheduledTaskLog)
            .where(ScheduledTaskLog.task_id == task_id)
            .order_by(ScheduledTaskLog.execution_time.desc(), ScheduledTaskLog.id.desc())
            .limit(100)
        )
    ).scalars().all()

    try:
        task_out = ScheduledTaskOut.model_validate(task)
    except Exception as exc:
        logger.exception(f"[ScheduledTaskDetail] 任务基本信息序列化失败 task_id={task_id}")
        raise BusinessException(ErrorCode.INTERNAL_ERROR, get_msg("TASK_SERIALIZATION_FAILED", error=f"{type(exc).__name__}: {exc}"))

    # 逐条序列化日志，单条异常不影响其它日志展示
    log_outs: list[ScheduledTaskLogOut] = []
    for log in logs:
        try:
            log_outs.append(ScheduledTaskLogOut.model_validate(log))
        except Exception as exc:
            logger.exception(
                f"[ScheduledTaskDetail] 日志序列化失败 task_id={task_id}, log_id={log.id}"
            )
            # 降级：构造一条占位没有死掉整个请求
            log_outs.append(
                ScheduledTaskLogOut(
                    id=log.id,
                    execution_time=log.execution_time,
                    trigger_type=getattr(log, "trigger_type", "scheduled") or "scheduled",
                    execution_status="failed",
                    duration=None,
                    result=f"[serialize error] {type(exc).__name__}: {exc}",
                )
            )

    return ScheduledTaskDetail(
        **task_out.model_dump(),
        execution_logs=log_outs,
    )


async def trigger_scheduled_tasks(
    db: AsyncSession,
    ids: list[int],
    operator: str | None = None,
) -> int:
    """
    批量手动触发任务。校验规则与前端期望一致：
    - 存在 running 的任务  → 抛 400，detail="BLOCKED_RUNNING:<task_types>"
    - 存在 disabled 的任务 → 抛 400，detail="BLOCKED_DISABLED:<task_types>"
    校验全部通过后再逐个触发。
    """
    if not ids:
        return 0

    tasks = (
        await db.execute(select(ScheduledTask).where(
            ScheduledTask.id.in_(ids),
            ScheduledTask.is_deleted.is_(False)
        ))
    ).scalars().all()

    found_ids = {t.id for t in tasks}
    missing = [i for i in ids if i not in found_ids]
    if missing:
        raise BusinessException(ErrorCode.SCHEDULED_TASK_IDS_NOT_FOUND, get_msg("SCHEDULED_TASK_IDS_NOT_FOUND", missing=missing))

    running = [t.task_type for t in tasks if t.execution_status == "running"]
    if running:
        raise BusinessException(ErrorCode.SCHEDULED_TASK_RUNNING_BLOCKED, get_msg("SCHEDULED_TASK_RUNNING_BLOCKED", task_types=running))
    disabled = [t.task_type for t in tasks if t.schedule_status != "enabled"]
    if disabled:
        raise BusinessException(ErrorCode.SCHEDULED_TASK_DISABLED_BLOCKED, get_msg("SCHEDULED_TASK_DISABLED_BLOCKED", task_types=disabled))

    # 延迟导入，避免循环依赖
    from app.jobs.scheduler import trigger_task_manual

    triggered = 0
    for task in tasks:
        await trigger_task_manual(db, task.task_type, operator=operator)
        triggered += 1

    return triggered


async def update_scheduled_task_cron(
    db: AsyncSession,
    task_id: int,
    cron_expression: str,
) -> ScheduledTask:
    """
    更新定时任务的 Cron 表达式。
    
    Args:
        db: 数据库会话
        task_id: 任务 ID
        cron_expression: 新的 Cron 表达式
    
    Returns:
        更新后的任务对象
    """
    task = await db.get(ScheduledTask, task_id)
    if task is None:
        raise BusinessException(ErrorCode.SCHEDULED_TASK_NOT_FOUND, get_msg("SCHEDULED_TASK_NOT_FOUND"))
    
    # 验证 Cron 表达式格式
    from app.jobs.scheduler import _build_cron_trigger
    try:
        _build_cron_trigger(cron_expression)
    except ValueError as e:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("INVALID_CRON_EXPRESSION", error=e))
    
    task.cron_expression = cron_expression
    await db.flush()
    await db.refresh(task)
    
    logger.info(f"[ScheduledTask] 更新任务 {task.task_type} 的 Cron 表达式: {cron_expression}")
    return task
