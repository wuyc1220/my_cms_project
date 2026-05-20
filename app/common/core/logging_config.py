"""
结构化日志配置

遵循中兴易监测日志规范，日志格式为 key=value||key=value 形式，
便于 agent-logs yaml 中的 bloblang 处理器解析。

动态配置项（通过 .env 环境变量）：
- LOG_ENABLED: 是否启用结构化日志（默认 true）
- LOG_DIR: 日志文件目录（默认 ./logs）
- LOG_LEVEL: 最低日志级别（默认 INFO）
- LOG_ROTATION: 日志轮转大小（默认 50MB）
- LOG_RETENTION: 日志保留天数（默认 30 days）
- LOG_FORMAT: 日志格式 structured(规范格式) / plain(原始格式)
- LOG_SERVICETYPE: 默认 servicetype（默认 other）
- LOG_DEVICE_TYPE: 设备类型标签（默认 cmsweb）
- LOG_PLATFORM_ID: 平台标识（默认 cms）
"""

import sys
from pathlib import Path

from loguru import logger

from app.common.core.log_enums import LogLevel, ServiceType


_DEFAULT_SERVICETYPE = "other"
_DEFAULT_DEVICE_TYPE = "cmsweb"
_DEFAULT_PLATFORM_ID = "cms"

_LEVEL_MAP = {
    "ACCESS": "ACCESS",
    "DEBUG": "DEBUG",
    "INFO": "INFO",
    "WARN": "WARNING",
    "WARNING": "WARNING",
    "ERROR": "ERROR",
    "OUT": "OUT",
}


def _escape_for_loguru(text: str) -> str:
    text = text.replace("{", "{{").replace("}", "}}")
    text = text.replace("<", "\\<").replace(">", "\\>")
    return text


def _structured_formatter(record: dict) -> str:
    extra = record["extra"]
    apiname = _escape_for_loguru(extra.get("apiname", ""))
    result = _escape_for_loguru(extra.get("result", ""))
    servicetype = _escape_for_loguru(extra.get("servicetype", _DEFAULT_SERVICETYPE))
    time_str = record["time"].strftime("%Y-%m-%d %H:%M:%S.") + f"{record['time'].microsecond // 1000:03d}"
    level = record["level"].name
    msg = _escape_for_loguru(record["message"])
    source = _escape_for_loguru(f"{record['name']}:{record['function']}:{record['line']}")
    return (
        f"time={time_str}||"
        f"level={level}||"
        f"servicetype={servicetype}||"
        f"apiname={apiname}||"
        f"result={result}||"
        f"msg={msg}||"
        f"devicetype={_DEFAULT_DEVICE_TYPE}||"
        f"platformid={_DEFAULT_PLATFORM_ID}||"
        f"source={source}\n"
    )


def _plain_formatter(record: dict) -> str:
    msg = _escape_for_loguru(record["message"])
    name = _escape_for_loguru(record["name"])
    func = _escape_for_loguru(record["function"])
    return (
        f"<green>{{time:YYYY-MM-DD HH:mm:ss.SSS}}</green> | "
        f"<level>{{level.name: <8}}</level> | "
        f"<cyan>{name}</cyan>:<cyan>{func}</cyan>:<cyan>{{line}}</cyan> - "
        f"<level>{msg}</level>\n"
    )


def configure_logging(
    log_enabled: bool = True,
    log_dir: str = "./logs",
    log_level: str = "INFO",
    log_rotation: str = "50 MB",
    log_retention: str = "30 days",
    log_format: str = "structured",
    default_servicetype: str = "other",
    device_type: str = "cmsweb",
    platform_id: str = "cms",
) -> None:
    """
    配置 Loguru 结构化日志

    Args:
        log_enabled: 是否启用结构化日志
        log_dir: 日志文件目录
        log_level: 最低日志级别
        log_rotation: 日志轮转大小
        log_retention: 日志保留天数
        log_format: structured / plain
        default_servicetype: 默认 servicetype
        device_type: 设备类型标签
        platform_id: 平台标识
    """
    global _DEFAULT_SERVICETYPE, _DEFAULT_DEVICE_TYPE, _DEFAULT_PLATFORM_ID
    _DEFAULT_SERVICETYPE = default_servicetype
    _DEFAULT_DEVICE_TYPE = device_type
    _DEFAULT_PLATFORM_ID = platform_id

    logger.remove()

    try:
        logger.level("OUT", no=25, color="<green>", icon="OUT")
    except TypeError:
        pass

    try:
        logger.level("ACCESS", no=15, color="<cyan>", icon="ACCESS")
    except TypeError:
        pass

    fmt_func = _structured_formatter if log_format == "structured" else _plain_formatter

    logger.add(
        sys.stderr,
        format=fmt_func,
        level=log_level,
        filter=lambda record: record["extra"].get("_sink", "all") == "all",
    )

    if not log_enabled:
        logger.bind(apiname="", result="").info("结构化日志文件输出已禁用")
        return

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    logger.add(
        str(log_path / "log_error.log"),
        format=fmt_func,
        level="ERROR",
        rotation=log_rotation,
        retention=log_retention,
        encoding="utf-8",
        filter=lambda record: record["extra"].get("_sink", "all") == "all",
    )

    logger.add(
        str(log_path / "log_out.log"),
        format=fmt_func,
        level="OUT",
        rotation=log_rotation,
        retention=log_retention,
        encoding="utf-8",
        filter=lambda record: record["level"].name == "OUT" and record["extra"].get("_sink", "all") == "all",
    )

    logger.add(
        str(log_path / "log_sql.log"),
        format=fmt_func,
        level="DEBUG",
        rotation=log_rotation,
        retention=log_retention,
        encoding="utf-8",
        filter=lambda record: record["extra"].get("_sink", "all") == "all" and _is_sql_log(record),
    )

    logger.add(
        str(log_path / "log_access.log"),
        format=fmt_func,
        level="ACCESS",
        rotation=log_rotation,
        retention=log_retention,
        encoding="utf-8",
        filter=lambda record: record["extra"].get("log_type") == "access" and record["extra"].get("_sink", "all") == "all",
    )

    logger.bind(apiname="", result="").info(
        "结构化日志配置完成 | dir={} level={} format={} servicetype={}",
        log_dir, log_level, log_format, default_servicetype,
    )


def _is_sql_log(record: dict) -> bool:
    msg = str(record.get("message", ""))
    return "SQL" in msg or "sql" in msg or "SELECT" in msg or "INSERT" in msg or "UPDATE" in msg or "DELETE" in msg


def log_business(
    level: str,
    servicetype: str,
    apiname: str,
    result: int = 0,
    msg: str = "",
    **kwargs,
) -> None:
    """
    打印业务日志的便捷方法

    Args:
        level: 日志级别 (ACCESS/DEBUG/INFO/WARN/ERROR/OUT)
               WARN 会自动映射为 Loguru 的 WARNING
        servicetype: 业务场景类型
        apiname: 接口/子场景名称
        result: 结果码 (0=成功, 非0=失败)
        msg: 日志消息
        **kwargs: 额外上下文信息
    """
    extra_msg = "||".join(f"{k}={v}" for k, v in kwargs.items()) if kwargs else ""
    full_msg = f"{msg}||{extra_msg}" if extra_msg else msg

    loguru_level = _LEVEL_MAP.get(level, level)

    logger.bind(apiname=apiname, result=str(result), servicetype=servicetype).log(
        loguru_level,
        full_msg,
    )
