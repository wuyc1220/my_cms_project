"""应用配置管理（环境变量 + .env 文件）"""
from zoneinfo import ZoneInfo

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """系统全局配置，优先读取 .env 文件，未设置时使用默认值"""
    # 基础信息
    app_name: str = "CMS内容管理平台"
    app_version: str = "0.1.0"
    database_url: str  # 数据库连接字符串
    secret_key: str = "454esds5d48rwe5rf4d12f48er4e5r1e515sd4w5e4we5w4e5wsa855fgr"  # JWT 签名密钥
    max_file_size: int = 21474836480  # 最大文件大小（2GB）
    access_token_expire_minutes: int = 480  # Token 有效期（8小时）

    # ============================================
    # 文件存储配置（支持 SFTP / FTP / Local）
    # ============================================
    storage_type: str = "sftp"  # 存储类型：sftp / ftp / local
    
    # 文件存储连接配置（根据 storage_type 自动选择协议）
    file_host: str = ""
    file_port: int = 22
    file_username: str = ""
    file_password: str = ""
    file_base_path: str = "/cms"
    
    # SFTP 专用配置
    sftp_strict_host_key_checking: bool = False
    
    # FTP 专用配置
    ftp_passive_mode: bool = True  # vsftpd 通常使用被动模式

    # 内置定时任务调度器（基于 APScheduler）
    scheduler_enabled: bool = True  # 是否启用内置定时调度器

    # 应用时区（影响定时任务、时间解析等，IANA 标准时区名）
    # 根据部署地区修改：新加坡用 Asia/Singapore，北京用 Asia/Shanghai
    app_timezone: str = "Asia/Shanghai"

    # 默认界面/消息语言（数据库配置 SYSTEM_UI_LANGUAGE 缺失或非法时的回退值）
    # 取值：cn / en。国际化部署可设为 en。
    default_language: str = "cn"

    # ============================================
    # 多中心部署配置
    # ============================================
    # 开关由数据库 config 表 MASTER_PLATFORM_ENABLED 控制，不在 .env 中配置
    multi_center_platform_id: str = "1"  # 当前平台ID，用于从 MULTI_CENTER_API_URLS JSON 中选取对应URL
    multi_center_check_interval: int = 10  # 主备检测间隔(秒)
    multi_center_api_timeout: int = 5  # API请求超时时间(秒)

    # IMDb 爬虫配置
    imdb_base_url: str = "https://www.imdb.com"  # IMDb 基础URL

    # SQLAlchemy 配置
    database_echo: bool = False  # 是否打印 SQL 语句到控制台 (True=开启, False=关闭)
    db_schema: str = "public"  # 数据库 schema 名称（如 jm_cms），部署时可修改

    # ============================================
    # 缓存配置
    # ============================================
    cache_type: str = "database"  # 缓存后端类型：database（cache_store表）/ redis（预留）

    # CORS 跨域配置
    cors_origins: str = "*"  # 允许的来源（逗号分隔，*表示允许所有）

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

    # ============================================
    # FTP URL 加密配置
    # ============================================
    ftp_url_encryption_enabled: bool = True  # 是否启用FTP URL加密
    ftp_url_encryption_key: str = "kX9mPq2wL5vN3zT8rY6tF1cB7hJ4dR2a"  # AES-256-GCM加密密钥（32字节）

    class Config:
        env_file = ".env"
        extra = "ignore"


# 全局配置实例
settings = Settings()

# 全局时区对象（供各模块直接引用，避免重复创建 ZoneInfo）
app_tz = ZoneInfo(settings.app_timezone)
