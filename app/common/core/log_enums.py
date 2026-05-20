"""
日志规范化枚举定义

遵循中兴易监测日志规范：
- level: 日志级别（ACCESS/DEBUG/INFO/WARN/ERROR/OUT）
- servicetype: 业务场景大类（控制在 20 个以内）
- apiname: 具体接口/子场景名称
"""

from enum import Enum


class LogLevel(str, Enum):
    ACCESS = "ACCESS"
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARN = "WARN"
    ERROR = "ERROR"
    OUT = "OUT"


class ServiceType(str, Enum):
    cntinjection = "cntinjection"
    datasync = "datasync"
    datadeal = "datadeal"
    dataquery = "dataquery"
    filesync = "filesync"
    userlogin = "userlogin"
    userinfo = "userinfo"
    system = "system"
    other = "other"


class ApiName(str, Enum):
    content_create = "content_create"
    content_update = "content_update"
    content_delete = "content_delete"
    content_publish = "content_publish"
    content_unpublish = "content_unpublish"
    content_offline = "content_offline"
    content_archive = "content_archive"
    c2_inject = "c2_inject"
    c2_callback = "c2_callback"
    license_create = "license_create"
    license_update = "license_update"
    license_delete = "license_delete"
    contract_create = "contract_create"
    contract_update = "contract_update"
    contract_delete = "contract_delete"
    provider_create = "provider_create"
    provider_update = "provider_update"
    provider_delete = "provider_delete"
    package_create = "package_create"
    package_update = "package_update"
    package_delete = "package_delete"
    publish_task_create = "publish_task_create"
    publish_task_callback = "publish_task_callback"
    publish_task_retry = "publish_task_retry"
    ingest_create = "ingest_create"
    ingest_update = "ingest_update"
    user_login = "user_login"
    user_logout = "user_logout"
    user_change_password = "user_change_password"
    config_update = "config_update"
    scheduled_task_execute = "scheduled_task_execute"
    metadata_quality_check = "metadata_quality_check"
    metadata_crawl = "metadata_crawl"
    health_check = "health_check"
    master_platform_check = "master_platform_check"


_ACCESS_LOG_METHODS = {"POST", "PUT", "DELETE", "PATCH", "GET"}

_ACCESS_LOG_WHITELIST = {
    "/api/v1/auth/captcha",
    "/api/v1/health",
    "/api/v1/health/master-platform",
}

_PATH_SERVICE_TYPE_MAP = {
    "/api/v1/contents": ServiceType.cntinjection,
    "/api/v1/metadata": ServiceType.cntinjection,
    "/api/v1/movies": ServiceType.cntinjection,
    "/api/v1/live": ServiceType.cntinjection,
    "/api/v1/vod": ServiceType.cntinjection,
    "/api/v1/pictures": ServiceType.filesync,
    "/api/v1/ingest-history": ServiceType.cntinjection,
    "/api/v1/publishes": ServiceType.cntinjection,
    "/api/v1/contracts": ServiceType.datadeal,
    "/api/v1/licenses": ServiceType.datadeal,
    "/api/v1/providers": ServiceType.datadeal,
    "/api/v1/packages": ServiceType.datadeal,
    "/api/v1/tasks": ServiceType.datadeal,
    "/api/v1/workflow": ServiceType.datadeal,
    "/api/v1/categories": ServiceType.dataquery,
    "/api/v1/tags": ServiceType.dataquery,
    "/api/v1/genres": ServiceType.dataquery,
    "/api/v1/casts": ServiceType.dataquery,
    "/api/v1/content-types": ServiceType.dataquery,
    "/api/v1/custom-fields": ServiceType.dataquery,
    "/api/v1/custom-tags": ServiceType.dataquery,
    "/api/v1/poster-sizes": ServiceType.dataquery,
    "/api/v1/metadata-sources": ServiceType.dataquery,
    "/api/v1/crawl-tasks": ServiceType.datasync,
    "/api/v1/metadata-quality-checks": ServiceType.datadeal,
    "/api/v1/scheduled-tasks": ServiceType.system,
    "/api/v1/configs": ServiceType.system,
    "/api/v1/dicts": ServiceType.system,
    "/api/v1/users": ServiceType.userinfo,
    "/api/v1/roles": ServiceType.system,
    "/api/v1/menus": ServiceType.system,
    "/api/v1/operation-logs": ServiceType.system,
    "/api/v1/auth": ServiceType.userlogin,
    "/api/v1/health": ServiceType.system,
    "/api/v1/dashboard": ServiceType.dataquery,
    "/api/v1/content-auth": ServiceType.system,
    "/api/v1/usage-limits": ServiceType.system,
    "/api/v1/sensitive-words": ServiceType.system,
    "/api/v1/attachments": ServiceType.filesync,
    "/soap/": ServiceType.cntinjection,
    "/commands/": ServiceType.filesync,
}


def resolve_service_type(path: str) -> str:
    for prefix, st in _PATH_SERVICE_TYPE_MAP.items():
        if path.startswith(prefix):
            return st.value
    return ServiceType.other.value


def resolve_apiname(path: str, method: str) -> str:
    parts = path.rstrip("/").split("/")
    resource = parts[-1] if parts else ""
    if resource.isdigit():
        resource = parts[-2] if len(parts) >= 2 else ""

    method_map = {
        "GET": "query",
        "POST": "create",
        "PUT": "update",
        "DELETE": "delete",
        "PATCH": "update",
    }
    action = method_map.get(method, "other")

    if not resource:
        return f"api_{action}"

    return f"{resource}_{action}"
