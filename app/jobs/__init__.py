"""
定时任务模块。

负责：
- 基于 APScheduler (AsyncIOScheduler) 的内置定时调度器生命周期管理。
- 集中维护业务定时任务（如 ContentOffline）。

对外暴露：
    start_scheduler()      — 启动内置调度器（在 FastAPI lifespan 中调用）。
    stop_scheduler()       — 停止内置调度器（在 FastAPI lifespan 中调用）。
    trigger_task_manual()  — 供 API 手动触发任务。
    reload_task()          — 任务配置变更后重新加载到调度器。
"""

from .scheduler import reload_task, start_scheduler, stop_scheduler, trigger_task_manual

__all__ = [
    "start_scheduler",
    "stop_scheduler",
    "trigger_task_manual",
    "reload_task",
]
