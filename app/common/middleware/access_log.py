"""
Access 日志中间件

遵循中兴日志规范，自动为每个 HTTP 请求打印 ACCESS 级别日志，
包含 servicetype、apiname、result、耗时等字段。

日志格式：time=xxx||level=ACCESS||servicetype=xxx||apiname=xxx||result=xxx||msg=xxx||...
"""

import time

from fastapi import Request, Response
from loguru import logger

from app.common.core.log_enums import (
    LogLevel,
    _ACCESS_LOG_WHITELIST,
    resolve_service_type,
    resolve_apiname,
)


async def access_log_middleware(request: Request, call_next) -> Response:
    """
    Access 日志中间件

    为每个 HTTP 请求自动记录结构化 access 日志，
    白名单路径（验证码、健康检查）不记录。
    """
    path = request.url.path
    method = request.method

    if path in _ACCESS_LOG_WHITELIST:
        return await call_next(request)

    if path.startswith("/docs") or path.startswith("/redoc") or path.startswith("/openapi"):
        return await call_next(request)

    servicetype = resolve_service_type(path)
    apiname = resolve_apiname(path, method)

    start_time = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

    status_code = response.status_code
    if 200 <= status_code < 400:
        result = 0
    elif 400 <= status_code < 500:
        result = status_code
    else:
        result = status_code

    logger.bind(
        apiname=apiname,
        result=str(result),
        servicetype=servicetype,
        log_type="access",
    ).log(
        LogLevel.ACCESS.value,
        "method={} path={} status={} elapsed={}ms",
        method,
        path,
        status_code,
        elapsed_ms,
    )

    return response
