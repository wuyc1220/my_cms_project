"""应用配置管理（环境变量 + .env 文件）"""
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """系统全局配置，优先读取 .env 文件，未设置时使用默认值"""
    # 基础信息
    app_name: str = "CMS内容管理平台"
    app_version: str = "0.1.0"
    database_url: str  # 数据库连接字符串
    secret_key: str  # JWT 签名密钥
    max_file_size: int = 21474836480  # 最大文件大小（2GB）
    access_token_expire_minutes: int = 480  # Token 有效期（8小时）

    # ============================================
    # 文件存储配置（支持 SFTP / FTP / Local）
    # ============================================
    storage_type: str = "sftp"  # 存储类型：sftp / ftp / local
    
    # SFTP 配置（storage_type=sftp 时使用）
    sftp_enabled: bool = True  # 向后兼容：映射到 storage_type=sftp
    sftp_host: str = ""
    sftp_port: int = 22
    sftp_username: str = ""
    sftp_password: str = ""
    sftp_base_path: str = "/cms"
    sftp_strict_host_key_checking: bool = False
    
    # FTP 配置（storage_type=ftp 时使用，vsftpd）
    ftp_host: str = ""
    ftp_port: int = 21
    ftp_username: str = ""
    ftp_password: str = ""
    ftp_base_path: str = "/cms"
    ftp_passive_mode: bool = True  # vsftpd 通常使用被动模式

    # 内置定时任务调度器（基于 APScheduler）
    scheduler_enabled: bool = True  # 是否启用内置定时调度器

    # 应用时区（影响定时任务、时间解析等，IANA 标准时区名）
    app_timezone: str = "Asia/Shanghai"

    # ============================================
    # 多中心部署配置
    # ============================================
    multi_center_enabled: bool = False  # 是否启用多中心模式 (true/false)
    multi_center_id: int = 1  # 当前中心ID (1, 2, 3...)
    multi_center_check_interval: int = 10  # 主备检测间隔(秒)
    multi_center_api_timeout: int = 5  # API请求超时时间(秒)
    multi_center_master_check_url: str = ""  # 主中心检测API地址(向后兼容)

    # IMDb 爬虫配置
    imdb_base_url: str = "https://www.imdb.com"  # IMDb 基础URL

    # SQLAlchemy 配置
    database_echo: bool = False  # 是否打印 SQL 语句到控制台 (True=开启, False=关闭)

    # ============================================
    # 结构化日志配置（对接中兴易监测平台）
    # ============================================
    log_enabled: bool = True  # 是否启用结构化日志文件输出
    log_dir: str = "./logs"  # 日志文件目录
    log_level: str = "INFO"  # 最低日志级别 (DEBUG/INFO/WARN/ERROR)
    log_rotation: str = "50 MB"  # 日志轮转大小
    log_retention: str = "30 days"  # 日志保留天数
    log_format: str = "structured"  # 日志格式: structured(规范key=value) / plain(原始格式)
    log_servicetype: str = "other"  # 默认 servicetype
    log_device_type: str = "cmsweb"  # 设备类型标签（对应 yaml 中 devicetype）
    log_platform_id: str = "cms"  # 平台标识（对应 yaml 中 platformid）

    class Config:
        env_file = ".env"
        extra = "ignore"


# 全局配置实例
settings = Settings()

# 全局时区对象（供各模块直接引用，避免重复创建 ZoneInfo）
app_tz = ZoneInfo(settings.app_timezone)
