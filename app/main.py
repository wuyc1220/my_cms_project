"""FastAPI 应用入口：生命周期管理、中间件、路由注册"""
# import sys
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

# if sys.platform == "win32":
#     import asyncio
#     asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import select

from app.routers import api_router
from app.config import settings
from app.database import AsyncSessionLocal
from app.common.core.i18n import set_default_lang, get_msg
from app.common.core.exceptions import BusinessException, ErrorCode
from app.common.core.logging_config import configure_logging
from app.common.services.master_platform_service import MasterPlatformService
from app.common.services.sensitive_check_service import SensitiveCheckService
from app.common.middleware.access_log import access_log_middleware
from app.common.middleware.language import LanguageMiddleware
from app.internal.cms_biz_system.models.config import Config
from app.jobs import start_scheduler, stop_scheduler
from app.plugins.manager import get_plugin_manager, shutdown_plugins
from app.soap import soap_router, file_server_router


_WRITE_METHODS = {"POST", "PUT", "DELETE", "PATCH"}

_WRITE_WHITELIST_PREFIXES = (
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/change-password",
    "/api/v1/auth/captcha",
    "/commands",
    "/soap",
)

_SENSITIVE_CHECK_PREFIXES = (
    "/api/v1/contents",
    "/api/v1/metadata",
    "/api/v1/movies",
    "/api/v1/live",
    "/api/v1/contracts",
    "/api/v1/licenses",
    "/api/v1/providers",
    "/api/v1/pictures",
    "/api/v1/categories",
    "/api/v1/tags",
    "/api/v1/custom-tags",
    "/api/v1/genres",
    "/api/v1/casts",
    "/api/v1/packages",
    "/api/v1/vod",
)


def _is_write_whitelisted(path: str) -> bool:
    for prefix in _WRITE_WHITELIST_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


def _should_check_sensitive(path: str, method: str) -> bool:
    if method not in ("POST", "PUT"):
        return False
    for prefix in _SENSITIVE_CHECK_PREFIXES:
        if path.startswith(prefix):
            return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理：启动时初始化语言和基础数据"""
    configure_logging(
        log_enabled=settings.log_enabled,
        log_dir=settings.log_dir,
        log_level=settings.log_level,
        log_rotation=settings.log_rotation,
        log_retention=settings.log_retention,
        log_format=settings.log_format,
        default_servicetype=settings.log_servicetype,
        device_type=settings.log_device_type,
        platform_id=settings.log_platform_id,
    )
    logger.info("CMS 后端启动中，正在初始化...")
    try:
        async with AsyncSessionLocal() as db:
            # 初始化全局默认语言配置（请求级语言由 LanguageMiddleware 按 Accept-Language 设置）
            lang_config = await db.execute(select(Config).where(Config.config_key == "SYSTEM_UI_LANGUAGE"))
            lang_row = lang_config.scalar_one_or_none()
            system_lang = (lang_row.config_value if lang_row else "").strip().lower()
            if system_lang not in {"cn", "en"}:
                system_lang = settings.default_language
            set_default_lang(system_lang)
            logger.info(f"默认语言初始化完成: {system_lang}")

            # 加载 SOAP 配置（从数据库覆盖 .env）
            from app.soap.config import load_soap_config_from_db
            await load_soap_config_from_db(db)
            logger.info("SOAP 配置加载完成")
    except Exception as e:
        logger.error(f"初始化过程出错（服务仍会启动，但部分功能可能不可用）: {e}")

    # 初始化主中心状态服务
    try:
        master_service = MasterPlatformService.get_instance()
        async with AsyncSessionLocal() as db:
            await master_service.load_config(db)
        logger.info(
            "【多中心】主中心状态服务初始化完成 | enabled={} is_master={}",
            master_service.enabled,
            master_service.is_master,
        )
    except Exception as e:
        logger.warning("【多中心】主中心状态服务初始化失败（默认为主中心）: {}", e)

    # 启动内置定时调度器（由 SCHEDULER_ENABLED 开关控制）
    try:
        await start_scheduler()
    except Exception as e:
        logger.error(f"内置定时调度器启动异常（主服务继续运行）: {e}")

    # 启动缓存过期清理后台任务（每10分钟清理 cache_store 表过期记录）
    try:
        from app.jobs.cache_cleaner import run_cache_cleaner
        asyncio.create_task(run_cache_cleaner())
        logger.info("缓存过期清理任务已启动 | interval=600秒")
    except Exception as e:
        logger.error(f"缓存过期清理任务启动异常: {e}")
    
    # 启动多中心主备检测任务（由数据库 config 表 MASTER_PLATFORM_ENABLED 控制）
    if master_service.enabled:
        try:
            from app.jobs.multi_center_check_service import run_multi_center_check
            asyncio.create_task(run_multi_center_check())
            logger.info(
                f"【多中心】主备检测任务已启动 | interval={settings.multi_center_check_interval}秒"
            )
        except Exception as e:
            logger.error(f"多中心主备检测任务启动异常: {e}")

    # 初始化爬虫插件管理器
    try:
        get_plugin_manager()
        logger.info("爬虫插件管理器初始化完成")
    except Exception as e:
        logger.error(f"爬虫插件管理器初始化异常（主服务继续运行）: {e}")

    logger.info("CMS 后端启动完成，开始接受请求")
    yield
    # 应用退出时关闭爬虫插件
    try:
        await shutdown_plugins()
    except Exception as e:
        logger.warning(f"爬虫插件关闭异常: {e}")
    # 应用退出时停止内置调度器
    try:
        await stop_scheduler()
    except Exception as e:
        logger.warning(f"内置定时调度器关闭异常: {e}")
    logger.info("CMS 后端正在关闭")


# 创建 FastAPI 应用实例
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    lifespan=lifespan,
)

# 配置 CORS 中间件
_cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
# 使用 * 时不能同时设置 credentials=true（浏览器会拒绝）
_cors_wildcard = "*" in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _cors_wildcard else _cors_origins,
    allow_credentials=not _cors_wildcard,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["captcha-id"],
)


@app.middleware("http")
async def access_log_middleware_wrapper(request: Request, call_next):
    return await access_log_middleware(request, call_next)


@app.middleware("http")
async def master_platform_middleware(request: Request, call_next):
    """
    多中心写操作拦截中间件

    非主中心时，所有写操作（POST/PUT/DELETE/PATCH）返回 403，
    仅白名单内的路径（登录/登出/改密/验证码）不受限制。
    当前默认为主中心（MASTER_PLATFORM_ENABLED=false），中间件不拦截任何请求。
    """
    if request.method in _WRITE_METHODS and not _is_write_whitelisted(request.url.path):
        service = MasterPlatformService.get_instance()
        if not await service.is_current_master():
            logger.warning(
                "非主中心写操作被拦截 | method={} path={}",
                request.method,
                request.url.path,
            )
            return JSONResponse(
                status_code=403,
                content={
                    "error_code": ErrorCode.NOT_MASTER_PLATFORM.value,
                    "message": get_msg("NOT_MASTER_PLATFORM"),
                    "detail": get_msg("NOT_MASTER_PLATFORM"),
                },
            )

    response = await call_next(request)

    service = MasterPlatformService.get_instance()
    response.headers["X-Master-Platform"] = str(service.is_master).lower()
    response.headers["X-Master-Check-Enabled"] = str(service.enabled).lower()
    return response


@app.middleware("http")
async def sensitive_word_check_middleware(request: Request, call_next):
    """
    敏感词检查中间件

    对业务内容相关的 POST/PUT 请求，读取请求体中的文本字段，
    与敏感词库（status=active 且 is_deleted=False）进行匹配。
    命中敏感词时返回 400，阻止提交。
    """
    if not _should_check_sensitive(request.url.path, request.method):
        return await call_next(request)

    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        return await call_next(request)

    body_bytes = await request.body()
    if not body_bytes:
        return await call_next(request)

    try:
        import json
        body = json.loads(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return await call_next(request)

    if isinstance(body, dict) and body:
        try:
            check_service = SensitiveCheckService.get_instance()
            async with AsyncSessionLocal() as db:
                has_sensitive = await check_service.check_dict(db, body)
                if has_sensitive:
                    msg = get_msg("SENSITIVE_WORD_DETECTED")
                    logger.warning(
                        "敏感词拦截 | method={} path={}",
                        request.method,
                        request.url.path,
                    )
                    return JSONResponse(
                        status_code=400,
                        content={
                            "error_code": ErrorCode.SENSITIVE_WORD_DETECTED.value,
                            "message": msg,
                            "detail": msg,
                        },
                    )
        except Exception as e:
            logger.error("敏感词检查异常（放行请求）: {}", e)

    async def _receive():
        return {"type": "http.request", "body": body_bytes}

    request = Request(request.scope, _receive)
    return await call_next(request)


# 语言中间件：最后注册 → 最外层 → 最先执行（先于 master_platform / sensitive_word 的 get_msg）。
# 纯 ASGI 实现，保证 ContextVar 正确传播到下游中间件与 endpoint。
app.add_middleware(LanguageMiddleware)

# 注册 API 路由
app.include_router(api_router, prefix="/api/v1")

# 注册 SOAP 内容分发路由
app.include_router(soap_router)
app.include_router(file_server_router)


# Pydantic/422 字段级验证错误消息映射（key → i18n key）
# 按 Pydantic v2 常见 type 和 msg 模式映射
PYDANTIC_VALIDATION_MSG_MAP = {
    "missing": "VALIDATION_FIELD_REQUIRED",
    "field required": "VALIDATION_FIELD_REQUIRED",
    "value_error.missing": "VALIDATION_FIELD_REQUIRED",
    "type_error.integer": "VALIDATION_VALUE_NOT_INTEGER",
    "type_error.float": "VALIDATION_VALUE_NOT_NUMBER",
    "type_error.int": "VALIDATION_VALUE_NOT_INTEGER",
    "value is not a valid integer": "VALIDATION_VALUE_NOT_INTEGER",
    "value is not a valid float": "VALIDATION_VALUE_NOT_NUMBER",
    "value is not a valid number": "VALIDATION_VALUE_NOT_NUMBER",
    "type_error.bool": "VALIDATION_BOOL_TYPE",
    "value is not a valid boolean": "VALIDATION_BOOL_TYPE",
    "type_error.list": "VALIDATION_LIST_TYPE",
    "value is not a valid list": "VALIDATION_LIST_TYPE",
    "type_error.dict": "VALIDATION_DICT_TYPE",
    "value is not a valid dict": "VALIDATION_DICT_TYPE",
    "type_error.str": "VALIDATION_STRING_TYPE",
    "value is not a valid string": "VALIDATION_STRING_TYPE",
    "type_error.json": "VALIDATION_JSON_INVALID",
    "json_invalid": "VALIDATION_JSON_INVALID",
    "value_error.jsondecode": "VALIDATION_JSON_INVALID",
    "JSON decode error": "VALIDATION_JSON_INVALID",
    "string_too_short": "VALIDATION_VALUE_TOO_SHORT",
    "string_too_long": "VALIDATION_VALUE_TOO_LONG",
    "string_pattern_mismatch": "VALIDATION_PATTERN_MISMATCH",
    "greater_than_equal": "VALIDATION_NUMBER_TOO_SMALL",
    "less_than_equal": "VALIDATION_NUMBER_TOO_LARGE",
    "enum": "VALIDATION_ENUM_INVALID",
    "value is not a valid enumeration member": "VALIDATION_ENUM_INVALID",
    "type_error": "VALIDATION_TYPE_ERROR",
}


def _translate_pydantic_errors(detail: list) -> str:
    """将 Pydantic 422 错误列表中的 msg 映射为本地化消息，保留 loc 字段信息。"""
    parts = []
    for d in detail:
        if not isinstance(d, dict):
            parts.append(str(d))
            continue
        loc = d.get("loc", [])
        # 去掉 FastAPI 固定的 "body" 前缀，仅保留字段路径，便于用户定位具体字段
        loc_str = ".".join(str(p) for p in loc if p != "body") if isinstance(loc, list) else str(loc)
        original_msg = d.get("msg", "")
        error_type = d.get("type", "")
        ctx = d.get("ctx", {})

        i18n_key = PYDANTIC_VALIDATION_MSG_MAP.get(error_type) or PYDANTIC_VALIDATION_MSG_MAP.get(original_msg)
        if i18n_key:
            # 从 ctx 中提取消息占位符参数（pattern/expected/max_length/min_length/le/ge）
            params = {}
            if isinstance(ctx, dict):
                for k in ("pattern", "expected", "max_length", "min_length", "le", "ge"):
                    if k in ctx:
                        params[k] = ctx[k]
            translated = get_msg(i18n_key, **params)
        else:
            translated = original_msg
        parts.append(f"{loc_str}: {translated}" if loc_str else translated)
    return "; ".join(parts)


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """全局 HTTP 异常处理器 - 统一响应格式（含 Pydantic 422 字段级 i18n）"""
    detail = exc.detail
    if isinstance(detail, list):
        detail = _translate_pydantic_errors(detail)
    # 500 状态使用 i18n 翻译兜底（防止硬编码中文消息）
    message = get_msg("INTERNAL_ERROR") if exc.status_code == 500 else detail
    logger.warning(
        "HTTP异常 | method={} path={} status={} detail={}",
        request.method,
        request.url.path,
        exc.status_code,
        detail,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": f"HTTP_{exc.status_code}", "message": message, "detail": message},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    """全局请求校验异常处理器 - 422 统一响应格式（含字段路径与本地化消息）"""
    message = _translate_pydantic_errors(exc.errors())
    logger.warning(
        "参数校验异常 | method={} path={} detail={}",
        request.method,
        request.url.path,
        message,
    )
    return JSONResponse(
        status_code=422,
        content={"error_code": "HTTP_422", "message": message, "detail": message},
    )


@app.exception_handler(BusinessException)
async def business_exception_handler(request: Request, exc: BusinessException):
    """全局业务异常处理器"""
    error_code = exc.error_code.value if hasattr(exc.error_code, "value") else exc.error_code
    logger.warning(
        "业务异常 | method={} path={} code={} message={}",
        request.method,
        request.url.path,
        error_code,
        exc.message,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error_code": error_code, "message": exc.message, "detail": exc.message},
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """全局系统异常处理器"""
    logger.exception("系统异常 | method={} path={} error={}", request.method, request.url.path, exc)
    return JSONResponse(
        status_code=500,
        content={"error_code": ErrorCode.INTERNAL_ERROR.value, "message": get_msg("INTERNAL_ERROR"), "detail": get_msg("INTERNAL_ERROR")},
    )


@app.get("/")
async def root():
    """健康检查入口"""
    return {"name": settings.app_name, "version": settings.app_version, "docs": "/docs"}


@app.get("/cms/webx/getValid")
async def get_valid():
    """OPS 健康检查接口：校验数据库连接是否正常"""
    from sqlalchemy import text
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "error", "database": "disconnected"},
        )
