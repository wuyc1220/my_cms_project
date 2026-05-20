"""
主中心状态管理服务

多中心部署架构下，系统需要判断当前中心是否为主中心：
- 主中心：读写权限不受限制
- 非主中心：只有读权限，所有写操作被拦截

配置项（二选一）：
1. 向后兼容模式：MASTER_PLATFORM_URLS = "http://ip:port/iptvslcs/getddbmasterplatform"
2. 多中心模式：MULTI_CENTER_API_URLS = '{"1": "http://ip1:port1/...", "2": "http://ip2:port2/..."}'
   同时需要配置：MULTI_CENTER_ID = 1
"""

import json
from datetime import datetime, timezone

import httpx
from loguru import logger

from app.config import settings


class MasterPlatformService:
    _instance = None

    def __init__(self):
        self._enabled: bool = False
        self._is_master: bool = True
        self._check_url: str = ""
        self._api_urls: dict = {}  # 多中心API地址映射
        self._center_id: int = 1  # 当前中心ID
        self._last_check_time: datetime | None = None
        self._last_check_result: dict | None = None
        self._last_error: str | None = None
        self._consecutive_failures: int = 0  # 连续失败次数

    @classmethod
    def get_instance(cls) -> "MasterPlatformService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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
        """加载多中心配置"""
        from app.internal.cms_biz_system.services.config_service import get_config_value

        # 1. 检查是否启用 (支持两个配置项)
        enabled_str = await get_config_value(db, "MASTER_PLATFORM_ENABLED", "false")
        if enabled_str.lower() not in ("true", "1", "yes"):
            enabled_str = await get_config_value(db, "MULTI_CENTER_ENABLED", "false")
        
        self._enabled = enabled_str.lower() in ("true", "1", "yes")

        # 2. 获取当前中心ID (多中心模式)
        center_id_str = await get_config_value(db, "MULTI_CENTER_ID", "1")
        try:
            self._center_id = int(center_id_str)
        except ValueError:
            logger.warning(f"无效的中心ID: {center_id_str}, 使用默认值 1")
            self._center_id = 1

        # 3. 获取 API URL (优先使用多中心配置)
        api_urls_str = await get_config_value(db, "MULTI_CENTER_API_URLS", "")
        
        if api_urls_str:
            # 多中心模式: JSON格式
            try:
                self._api_urls = json.loads(api_urls_str)
                self._check_url = self._api_urls.get(str(self._center_id), "")
                logger.info(
                    f"主中心配置加载完成 [多中心模式] | enabled={self._enabled}, "
                    f"center_id={self._center_id}, url={self._check_url or '(未配置)'}"
                )
            except json.JSONDecodeError as e:
                logger.error(f"MULTI_CENTER_API_URLS JSON解析失败: {e}")
                self._api_urls = {}
                self._check_url = ""
        else:
            # 向后兼容: 单URL模式
            self._check_url = await get_config_value(db, "MASTER_PLATFORM_URLS", "")
            logger.info(
                f"主中心配置加载完成 [单中心模式] | enabled={self._enabled}, "
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
            logger.warning("主中心检测URL未配置,默认为主中心")
            self._is_master = True
            self._last_check_time = datetime.now(timezone.utc)
            self._last_error = "检测URL未配置"
            return True
        
        try:
            # 调用业务平台API
            timeout = settings.multi_center_api_timeout
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
                        f"主中心检测接口返回错误 | error={self._last_error}, "
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
                        f"主中心状态检测完成 | is_master={self._is_master}, "
                        f"master_plat_id={data.get('masterplatid')}, "
                        f"update_time={data.get('updatetime')}"
                    )
                    
        except httpx.TimeoutException:
            # 超时:保持上次状态,不盲目切换
            self._last_error = f"接口超时 (>{timeout}秒)"
            self._last_check_time = datetime.now(timezone.utc)
            self._consecutive_failures += 1
            
            logger.error(
                f"主中心检测接口超时 | consecutive_failures={self._consecutive_failures}, "
                f"保持上次状态 is_master={self._is_master}"
            )
            # 不修改 _is_master,保持上次状态
            
        except Exception as exc:
            # 其他异常:保持上次状态
            self._last_error = f"接口不可达: {exc}"
            self._last_check_time = datetime.now(timezone.utc)
            self._consecutive_failures += 1
            
            logger.error(
                f"主中心检测接口异常 | consecutive_failures={self._consecutive_failures}, "
                f"保持上次状态 is_master={self._is_master}"
            )
            # 不修改 _is_master,保持上次状态
        
        # 连续失败告警
        if self._consecutive_failures >= 10:
            logger.critical(
                f"主中心检测接口连续失败 {self._consecutive_failures} 次, "
                f"请检查网络或业务平台状态!"
            )
        
        return self._is_master

    async def is_current_master(self) -> bool:
        """
        判断当前中心是否为主中心

        - 未启用检测时始终返回 True（默认主中心）
        - 启用后每次调用都实时检测（强一致性，不缓存）
        """
        if not self._enabled:
            return True
        return await self.check_master_status()

    def get_status_info(self) -> dict:
        """获取主备状态信息"""
        return {
            "enabled": self._enabled,
            "is_master": self._is_master if self._enabled else True,
            "center_id": self._center_id,
            "check_url": self._check_url or "(未配置)",
            "api_urls": self._api_urls,
            "last_check_time": self._last_check_time.isoformat() if self._last_check_time else None,
            "last_check_result": self._last_check_result,
            "last_error": self._last_error,
            "consecutive_failures": self._consecutive_failures,
        }
