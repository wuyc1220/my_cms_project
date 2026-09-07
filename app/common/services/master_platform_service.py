"""
主中心状态管理服务

多中心部署架构下，系统需要判断当前中心是否为主中心：
- 主中心：读写权限不受限制
- 非主中心：只有读权限，所有写操作被拦截

配置方式（数据库 config 表）：
1. MASTER_PLATFORM_ENABLED = "true"   — 开关，启用主备检测
2. MULTI_CENTER_API_URLS = '{"1": "http://..."}'  — JSON格式, 按 platformid 映射检测接口URL
   运行时通过 MULTI_CENTER_PLATFORM_ID 环境变量选取对应URL
"""

from datetime import datetime, timezone
import json

import httpx
from loguru import logger

from app.config import settings


class MasterPlatformService:
    _instance = None

    def __init__(self):
        self._enabled: bool = False
        self._is_master: bool = True
        self._platform_id: str = "1"
        self._check_url: str = ""
        self._last_check_time: datetime | None = None
        self._last_check_result: dict | None = None
        self._last_error: str | None = None
        self._consecutive_failures: int = 0

    @classmethod
    def get_instance(cls) -> "MasterPlatformService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def platform_id(self) -> str:
        return self._platform_id

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def is_master(self) -> bool:
        return self._is_master

    @property
    def last_check_time(self) -> datetime | None:
        return self._last_check_time

    @property
    def last_error(self) -> str | None:
        return self._last_error

    async def load_config(self, db) -> None:
        """
        加载主备检测配置（从数据库 config 表读取）

        1. 读取 MASTER_PLATFORM_ENABLED 判断是否启用
        2. 读取 MULTI_CENTER_API_URLS, 按 JSON 格式解析,
           通过 settings.multi_center_platform_id 选取对应平台的 URL
        3. 读取 MULTI_CENTER_API_TIMEOUT 设置超时时间
        4. 若 MULTI_CENTER_API_URLS 不是合法 JSON 或为纯字符串,
           则直接作为 URL 使用（向后兼容）
        """
        from app.internal.cms_biz_system.services.config_service import get_config_value, get_config_int

        enabled_str = await get_config_value(db, "MASTER_PLATFORM_ENABLED", "false")
        self._enabled = enabled_str.lower() in ("true", "1", "yes")

        raw_urls = await get_config_value(db, "MULTI_CENTER_API_URLS", "")

        # 读取当前平台编号（优先数据库，降级到 .env）
        self._platform_id = await get_config_value(db, "MULTI_CENTER_PLATFORM_ID", settings.multi_center_platform_id)

        # 尝试按 JSON 格式解析, 通过 platformid 提取对应 URL
        platform_id = self._platform_id
        self._check_url = ""
        try:
            urls_map = json.loads(raw_urls)
            if isinstance(urls_map, dict):
                self._check_url = urls_map.get(platform_id, "")
                if not self._check_url:
                    logger.warning(
                        f"MULTI_CENTER_API_URLS 中未找到 platformid={platform_id} 的URL, "
                        f"可用key: {list(urls_map.keys())}"
                    )
            else:
                # JSON 解析成功但不是 dict (如纯字符串), 直接使用
                self._check_url = str(urls_map)
        except (json.JSONDecodeError, TypeError, ValueError):
            # 非 JSON 格式, 直接作为 URL 使用 (向后兼容纯字符串配置)
            self._check_url = raw_urls

        # 加载超时配置（优先数据库，降级到 .env）
        self._timeout = await get_config_int(db, "MULTI_CENTER_API_TIMEOUT", default_value=settings.multi_center_api_timeout)

        logger.info(
            f"【多中心】主中心配置加载完成 | enabled={self._enabled}, "
            f"platform_id={platform_id}, "
            f"url={self._check_url or '(未配置)'}"
        )

    async def check_master_status(self) -> bool:
        """
        检测当前中心是否为主中心（强一致性，每次调用都实时检测）
        
        外部接口返回格式:
        {
            "returncode": "0",
            "errmsg": "success",
            "masterplatid": "1",
            "ismasterplat": "1",  # 1=主中心, 0=备中心
            "updatetime": "20260513143022"
        }
        """
        if not self._enabled:
            # 未启用多中心模式,默认就是主中心
            self._is_master = True
            self._last_check_time = datetime.now(timezone.utc)
            self._last_error = None
            return True
        
        if not self._check_url:
            logger.warning("【多中心】主中心检测URL未配置,默认为主中心")
            self._is_master = True
            self._last_check_time = datetime.now(timezone.utc)
            self._last_error = "检测URL未配置"
            return True
        
        try:
            # 调用业务平台API
            timeout = getattr(self, '_timeout', settings.multi_center_api_timeout)
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(self._check_url)
                resp.raise_for_status()
                data = resp.json()
                
                # 解析响应
                if data.get("returncode") != "0":
                    # 接口返回错误
                    self._is_master = False
                    self._last_error = f"接口返回错误: {data.get('errmsg', 'unknown')}"
                    self._last_check_time = datetime.now(timezone.utc)
                    self._last_check_result = data
                    self._consecutive_failures += 1
                    
                    logger.warning(
                        f"【多中心】主中心检测接口返回错误 | error={self._last_error}, "
                        f"consecutive_failures={self._consecutive_failures}"
                    )
                else:
                    # 成功获取主备状态
                    self._is_master = data.get("ismasterplat") == "1"
                    self._last_error = None
                    self._last_check_time = datetime.now(timezone.utc)
                    self._last_check_result = data
                    self._consecutive_failures = 0  # 重置失败计数
                    
                    logger.info(
                        f"【多中心】主中心状态检测完成 | is_master={self._is_master}, "
                        f"master_plat_id={data.get('masterplatid')}, "
                        f"update_time={data.get('updatetime')}"
                    )
                    
        except httpx.TimeoutException:
            # 超时:保持上次状态,不盲目切换
            self._last_error = f"接口超时 (>{timeout}秒)"
            self._last_check_time = datetime.now(timezone.utc)
            self._consecutive_failures += 1
            
            logger.error(
                f"【多中心】主中心检测接口超时 | consecutive_failures={self._consecutive_failures}, "
                f"保持上次状态 is_master={self._is_master}"
            )
            # 不修改 _is_master,保持上次状态
            
        except Exception as exc:
            # 其他异常:保持上次状态
            self._last_error = f"接口不可达: {exc}"
            self._last_check_time = datetime.now(timezone.utc)
            self._consecutive_failures += 1
            
            logger.error(
                f"【多中心】主中心检测接口异常 | consecutive_failures={self._consecutive_failures}, "
                f"保持上次状态 is_master={self._is_master}"
            )
            # 不修改 _is_master,保持上次状态
        
        # 连续失败告警
        if self._consecutive_failures >= 10:
            logger.critical(
                f"【多中心】主中心检测接口连续失败 {self._consecutive_failures} 次, "
                f"请检查网络或业务平台状态!"
            )
        
        return self._is_master

    async def is_current_master(self) -> bool:
        """
        判断当前中心是否为主中心（读缓存，由后台定时任务更新）

        - 未启用检测时始终返回 True（默认主中心）
        - 启用后返回定时任务缓存的 _is_master，不实时发请求
        """
        if not self._enabled:
            return True
        return self._is_master

    def get_status_info(self) -> dict:
        """获取主备状态信息"""
        return {
            "enabled": self._enabled,
            "is_master": self._is_master if self._enabled else True,
            "check_url": self._check_url or "(未配置)",
            "last_check_time": self._last_check_time.isoformat() if self._last_check_time else None,
            "last_check_result": self._last_check_result,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
        }
