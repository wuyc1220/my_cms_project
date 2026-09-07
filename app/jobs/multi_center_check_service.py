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
    - 数据库 config 表 MASTER_PLATFORM_ENABLED = true
    - 间隔由 MULTI_CENTER_CHECK_INTERVAL 配置 (默认10秒)
    """
    service = MasterPlatformService.get_instance()

    if not service.enabled:
        logger.info("【多中心】主备检测未启用, 默认为主中心模式")
        return

    # 优先从数据库读取配置，降级到 .env
    from app.database import async_session
    from app.internal.cms_biz_system.services.config_service import get_config_int
    
    interval = settings.multi_center_check_interval  # .env 默认值
    try:
        async with async_session() as db:
            db_interval = await get_config_int(db, "MULTI_CENTER_CHECK_INTERVAL", default_value=interval)
            if db_interval != interval:
                logger.info(f"【多中心】检测间隔从数据库加载: {db_interval}秒 (覆盖.env: {interval}秒)")
                interval = db_interval
    except Exception as e:
        logger.warning(f"【多中心】从数据库加载 MULTI_CENTER_CHECK_INTERVAL 失败，使用 .env 默认值: {e}")
    
    platform_id = service.platform_id

    logger.info(
        f"【多中心】主备检测任务已启动 | platform_id={platform_id}, "
        f"检测间隔={interval}秒, 检测URL={service._check_url}"
    )

    check_count = 0
    while True:
        try:
            is_master = await service.check_master_status()
            check_count += 1

            # 每 6 次（约60秒）输出一次状态摘要，方便确认功能在正常运行
            if check_count % 6 == 0:
                status_info = service.get_status_info()
                logger.info(
                    f"【多中心】状态摘要 | platform_id={platform_id}, "
                    f"is_master={is_master}, "
                    f"检测次数={check_count}, "
                    f"连续失败次数={status_info['consecutive_failures']}, "
                    f"上次检测时间={status_info['last_check_time']}"
                )
        except Exception as e:
            logger.error(f"【多中心】主备检测任务异常: {e}")

        await asyncio.sleep(interval)
