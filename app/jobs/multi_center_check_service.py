"""
多中心主备检测定时任务

每 N 秒执行一次主备状态检测
这个任务在后台持续运行,不在 scheduled_task 表中注册
因为它是一个高频任务,不适合用cron表达式
"""
import asyncio

from loguru import logger

from app.common.services.master_platform_service import MasterPlatformService
from app.config import settings


async def run_multi_center_check():
    """
    定时检测主备状态
    
    运行条件:
    - multi_center_enabled = true
    - 间隔由 multi_center_check_interval 配置 (默认10秒)
    """
    if not settings.multi_center_enabled:
        logger.info("多中心模式未启用,跳过主备检测")
        return
    
    interval = settings.multi_center_check_interval
    service = MasterPlatformService.get_instance()
    
    logger.info(f"启动多中心主备检测任务 | 间隔={interval}秒")
    
    while True:
        try:
            await service.check_master_status()
        except Exception as e:
            logger.error(f"主备检测任务异常: {e}")
        
        # 等待下一次检测
        await asyncio.sleep(interval)
