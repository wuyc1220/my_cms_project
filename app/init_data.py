"""应用启动时数据初始化（管理员、配置、使用限额、字典）"""
import json

from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.core import UserStatus, RoleStatus, DictStatus
from app.internal.cms_biz_system.models.config import Config
from app.internal.cms_biz_system.models.dict import DictNode
from app.internal.cms_biz_system.models.scheduled_task import ScheduledTask
from app.internal.cms_biz_system.models.user import Role, User, UserRole
from app.internal.cms_biz_system.models.usage_limit import UsageLimit


# ==================== 管理员与角色初始化 ====================

_PRESET_ROLES = [
    ("管理员", "ADMIN"),
    ("内容编排", "CONTENT_EDIT"),
    ("L1内容审核", "REVIEW_L1"),
    ("L2内容审核", "REVIEW_L2"),
    ("L3内容审核", "REVIEW_L3"),
    ("任务分配", "TASK_ASSIGN"),
]


async def init_admin(db: AsyncSession) -> None:
    """初始化 admin 用户和预设角色（幂等）"""
    result = await db.execute(
        select(User)
        .where(User.username == "admin", User.is_deleted.is_(False))
        .options(selectinload(User.roles).selectinload(UserRole.role))
    )
    admin = result.scalar_one_or_none()
    if admin is None:
        pwd = CryptContext(schemes=["bcrypt"], deprecated="auto").hash("admin123")
        admin = User(
            username="admin",
            password_hash=pwd,
            display_name="系统管理员",
            status=UserStatus.ACTIVE.value,
        )
        db.add(admin)
        await db.flush()

    existing_roles = (await db.execute(select(Role).where(Role.is_deleted.is_(False)))).scalars().all()
    role_map = {role.code: role for role in existing_roles}
    for name, code in _PRESET_ROLES:
        if code not in role_map:
            role = Role(name=name, code=code, status=RoleStatus.ACTIVE.value, is_system=True)
            db.add(role)
            await db.flush()
            role_map[code] = role

    admin_role = role_map["ADMIN"]
    has_admin_role = any(link.role_id == admin_role.id for link in admin.roles)
    if not has_admin_role:
        db.add(UserRole(user_id=admin.id, role_id=admin_role.id))

    await db.commit()


# ==================== 系统配置初始化 ====================

_PRESET_CONFIGS = [
    ("SYSTEM_UI_LANGUAGE", "CMS 系统界面语言", "en", "决定界面统一展示英文或中文，修改后实时生效，不支持中英混合"),
    (
        "MOVIE_DEEPLINK_HINT",
        "Movie 元数据 Deeplink 提示语",
        "请输入有效的Deeplink地址，格式：scheme://host/path",
        "显示在 Movie 元数据编辑表单 Deeplink 字段的占位提示语",
    ),
    (
        "PHYSICAL_DEEPLINK_HINT",
        "Physical Channel Deeplink 提示语",
        'Support flexible definition of JSON, with different URLs for different terminal types. For example: {"1": "xxx", "2": "xxx", "4": "xxx", "8": "xxx"}',
        "显示在 Physical Channel 添加表单 Deeplink 字段的占位提示语",
    ),
    ("LOGIN_CAPTCHA_EXPIRE", "登录图片验证码有效期", "5", "单位：分钟，超时需重新获取"),
    ("LOGIN_MAX_FAIL_COUNT", "最大登录失败次数", "5", "连续登录失败多少次后锁定账号"),
    ("LOGIN_LOCK_MINUTES", "账号锁定时间", "30", "账号锁定时长（分钟）"),
    ("PASSWORD_EXPIRE_DAYS", "密码过期天数", "90", "密码有效期（天），0表示永不过期"),
    ("SESSION_IDLE_TIMEOUT", "空闲超时时间", "30", "无操作自动退出时间（分钟）"),
    ("DEFAULT_REVIEW_LEVEL", "供应商默认审核层级", "L1", "新建供应商时\"内容审核层级\"字段的默认值，取字典 Content_review_level 的 code（None/L1/L2/L3）"),
    ("DEFAULT_PAGE_SIZE", "分页默认每页条数", "10", "各列表页默认每页显示条数"),
    ("UPLOAD_MAX_SIZE_MB", "文件上传最大大小", "500", "单个文件上传大小限制，单位：MB"),
    ("PUBLISH_RETRY_COUNT", "内容发布自动重试次数", "3", "发布失败后的最大自动重试次数"),
    ("TASK_TIMEOUT_HOURS", "任务超时告警时间", "48", "任务超过此时长未完成则触发告警，单位：小时"),
    ("MAINTENANCE_EMAIL", "系统维护通知邮箱", "admin@cms.example.com", "系统维护通知的接收邮箱地址"),
    ("PASSWORD_MIN_LENGTH", "密码最小长度", "8", "用户密码的最小字符长度要求"),
    ("NEAR_EXPIRY_DAYS", "内容临近过期预警天数", "7", "许可证到期日在该天数内的内容标记为临近过期，单位：天"),
    (
        "MASTER_PLATFORM_ENABLED",
        "主中心检测开关",
        "false",
        "是否启用多中心主从检测。false=始终为主中心（不拦截写操作），true=启用检测（需配置 MASTER_PLATFORM_URLS）",
    ),
    (
        "MASTER_PLATFORM_URLS",
        "主中心检测接口URL",
        "{}",
        'JSON格式: {"平台编号":"接口URL"}，例如 {"1":"http://ip1:port1/iptvslcs/getddbmasterplatform","2":"http://ip2:port2/iptvslcs/getddbmasterplatform"}',
    ),
]


async def init_configs(db: AsyncSession) -> None:
    """初始化系统配置参数（幂等，不覆盖用户修改的值）"""
    for key, name, value, desc in _PRESET_CONFIGS:
        existing = (await db.execute(select(Config).where(Config.config_key == key))).scalar_one_or_none()
        if existing is None:
            db.add(Config(config_key=key, config_name=name, config_value=value, description=desc, is_system=True))
        else:
            existing.config_name = name
            existing.description = desc
            existing.is_system = True
            # 不覆盖用户已修改的 config_value
    await db.commit()


# ==================== 使用限额初始化 ====================

_PRESET_USAGE_LIMITS = [
    ("supplier_count", -1, "供应商数量超过后，不允许添加新的供应商"),
    ("content_count", -1, "VOD内容(MOVIE/SEASON/SERIES)数量超过后，不允许添加新的内容"),
    ("storage_capacity", -1, "存放Movie、Picture等文件的大小超过后，不允许上传新的文件（单位：GB）"),
]


async def init_usage_limits(db: AsyncSession) -> None:
    """初始化使用限额（仅更新描述，不覆盖用户已修改的 limit_value）"""
    for limit_type, limit_value, desc in _PRESET_USAGE_LIMITS:
        existing = (
            await db.execute(select(UsageLimit).where(UsageLimit.limit_type == limit_type))
        ).scalar_one_or_none()
        if existing is None:
            db.add(UsageLimit(limit_type=limit_type, limit_value=limit_value, description=desc))
        else:
            existing.description = desc
    await db.commit()


# ==================== 定时任务初始化 ====================

# (task_type, description, cron_expression, schedule_status, execution_timeout, retry_count)
_PRESET_SCHEDULED_TASKS = [
    (
        "ContentOffline",
        "Content expiration automatic offline task",
        "1 0 0 * * *",  # 每日 00:00:01
        "enabled",
        300,
        3,
    ),
    (
        "ContentScheduledOffline",
        "Content scheduled takedown task",
        "* * * * *",  # 每分钟检查一次，有任务时自动连续处理
        "enabled",
        300,
        3,
    ),
    (
        "ContentPublish",
        "Content scheduled publish task",
        "* * * * *",  # 每分钟检查一次，有任务时自动连续处理
        "enabled",
        300,
        2,
    ),
    (
        "ContentArchive",
        "Content scheduled archive task",
        "* * * * *",  # 每分钟检查一次，循环处理
        "enabled",
        300,
        2,
    ),
    (
        "MetadataQualityCheck",
        "Metadata quality check task",
        "0 2 * * *",  # 每日 02:00
        "enabled",
        600,
        1,
    ),
]


async def init_scheduled_tasks(db: AsyncSession) -> None:
    """初始化定时任务配置（幂等）。不覆盖用户已改的 cron / schedule_status。"""
    for task_type, desc, cron_expr, schedule_status, timeout, retry in _PRESET_SCHEDULED_TASKS:
        existing = (
            await db.execute(select(ScheduledTask).where(ScheduledTask.task_type == task_type))
        ).scalar_one_or_none()
        if existing is None:
            db.add(
                ScheduledTask(
                    task_type=task_type,
                    description=desc,
                    cron_expression=cron_expr,
                    schedule_status=schedule_status,
                    execution_status="idle",
                    execution_timeout=timeout,
                    retry_count=retry,
                )
            )
        else:
            # 只追写 description，其余配置由用户可修改
            existing.description = desc
    await db.commit()


# ==================== 数据字典初始化 ====================

_PRESET_DICTS = [
    {
        "code": "Multi_Languages",
        "name": "多语言种类",
        "remark": "全系统多语言标签页语言列表；所有实体的 Language 语言选择器",
        "children": [("en", "English"), ("cn", "Chinese"), ("tr", "Turkish")],
    },
    {
        "code": "Language",
        "name": "内容语言",
        "remark": "内容元数据的 Language、AudioLang、SubtitleLang 字段候选项",
        "children": [("en", "English"), ("cn", "Chinese"), ("tr", "Turkish"), ("ar", "Arabic")],
    },
    {
        "code": "Category_Type",
        "name": "栏目类型",
        "remark": "栏目管理 CategoryType 字段候选项",
        "children": [("movie", "电影栏目"), ("season", "剧集栏目"), ("live", "直播栏目"), ("children", "儿童栏目")],
    },
    {
        "code": "Content_review_level",
        "name": "内容审核层级",
        "remark": "供应商默认审核层级；任务管理审核层级。code 与层级对应：None=免审、L1=一层、L2=二层、L3=三层。DEFAULT_REVIEW_LEVEL 配置项取此字典的 code 值",
        "children": [("None", "免审"), ("L1", "一层"), ("L2", "二层"), ("L3", "三层")],
    },
    {
        "code": "Movie_Type",
        "name": "影片类型",
        "remark": "MOVIE/EPISODE 元数据 Type 字段候选项",
        "children": [("action", "动作片"), ("comedy", "喜剧片"), ("drama", "剧情片"), ("horror", "惊悚片"), ("horror", "恐怖片"), ("animation", "动画片")],
    },
    {
        "code": "Package_Type",
        "name": "服务包类型",
        "remark": "服务包元数据 Package Type 字段候选项",
        "children": [("Main", "Main"), ("Conditional", "Conditional")],
    },
    {
        "code": "Platform",
        "name": "平台",
        "remark": "服务包、栏目管理中的 Platform 字段候选项",
        "children": [("IPTV", "IPTV"), ("Mobile", "Mobile"), ("SmartTV", "SmartTV"), ("Web", "Web")],
    },
    {
        "code": "Ingest_Status",
        "name": "Ingest 状态",
        "remark": "全系统 Ingest Status 字段候选项；代表内容生命周期各阶段，由系统根据流程规则自动切换，前端不提供手动修改入口",
        "children": [("None", "None"), ("WaitingForMaterials", "WaitingForMaterials"), ("InProgress", "InProgress"), ("ReadyForPublish", "ReadyForPublish"), ("Publishing", "Publishing"), ("PublishFailed", "PublishFailed"), ("Published", "Published"), ("NoActiveLicense", "NoActiveLicense"), ("Closed", "Closed")],
    },
    {
        "code": "Service_Type",
        "name": "服务类型",
        "remark": "许可证服务类型字段候选项",
        "children": [("SVOD", "SVOD（订阅制）"), ("TVOD", "TVOD（按次付费）"), ("EST", "EST（电子销售）")],
    },
    {
        "code": "Image_file_extensions",
        "name": "图片文件扩展名",
        "remark": "海报规格管理中的图片文件扩展名字段候选项；上传海报时校验文件格式",
        "children": [("jpg", "jpg"), ("jpeg", "jpeg"), ("png", "png"), ("webp", "webp")],
    },
    {
        "code": "BroadcastType",
        "name": "播出类型",
        "remark": "SCHEDULE 元数据 BroadcastType 字段候选项",
        "children": [("FirstBroadcast", "首播"), ("Reroadcast", "重播"), ("live", "直播")],
    },
    {
        "code": "AudioType",
        "name": "音频类型",
        "remark": "内容材料（Materials）中 AudioType 字段候选项",
        "children": [("Stereo", "Stereo"), ("Dolby", "Dolby"), ("DTS", "DTS")],
    },
    {
        "code": "Advice",
        "name": "内容提示/警告",
        "remark": "MOVIE/EPISODE/SCHEDULE 元数据 Advice 字段候选项",
        "children": [("Violence", "Violence"), ("Sexuality", "Sexuality"), ("Bad behaviours", "Bad behaviours")],
    },
    {
        "code": "RatingLevel",
        "name": "内容分级",
        "remark": "MOVIE/EPISODE/SCHEDULE 元数据 RatingLevel 字段候选项",
        "children": [("G", "G"), ("7+", "7+"), ("13+", "13+"), ("18+", "18+")],
    },
    {
        "code": "Sensitive_Word_Type",
        "name": "敏感词类型",
        "remark": "敏感词管理中 Type 字段候选项",
        "children": [("physical_attack", "人身攻击"), ("region", "地域歧视"), ("sexuality", "色情低俗"), ("political", "政治敏感")],
    },
    {
        "code": "mediaservice",
        "name": "媒体服务",
        "remark": "Physical Channel 媒体服务字段候选项",
        "children": [("OTT", "OTT"), ("IPTV", "IPTV"), ("DVB", "DVB")],
    },
    {
        "code": "Videoencode",
        "name": "频道编码",
        "remark": "Physical Channel 频道编码字段候选项",
        "children": [("0", "0"), ("H264", "H264"), ("H265", "H265")],
    },
    {
        "code": "Regions",
        "name": "地区",
        "remark": "许可证的 Regions 字段候选项",
        "children": [("ALL", "ALL")],
    },
    {
        "code": "ScreenFormat",
        "name": "屏幕格式",
        "remark": "内容材料（Materials）中 ScreenFormat 字段候选项",
        "children": [("0", "4x3"), ("1", "16x9")],
    },
    {
        "code": "Definition",
        "name": "定义",
        "remark": "内容材料（Materials）中 Definition 字段候选项",
        "children": [("SD", "SD"), ("HD", "HD"), ("4K", "4K")],
    },
    {
        "code": "VodType",
        "name": "Vod类型",
        "remark": "MOVIE/EPISODE/SEASON/SERIES 元数据 VodType 字段候选项",
        "children": [("EST", "EST"), ("TVOD", "TVOD"), ("SVOD", "SVOD")],
    },
    {
        "code": "Channel_type",
        "name": "频道类型",
        "remark": "CHANNEL 元数据 Type 字段候选项",
        "children": [("0", "直播"), ("1", "轮播频道"), ("3", "Masic"), ("4", "PIP"), ("6", "deeplink频道")],
    },
    {
        "code": "Content_Issue_Types",
        "name": "内容问题类型",
        "remark": "内容审核时 Content Issue Types 字段候选项",
        "children": [("missing_materials", "缺少素材"), ("missing_posters", "缺少片花"), ("incorrect_information", "信息不正确")],
    },
    {
        "code": "SeriesType",
        "name": "连续剧类型",
        "remark": "SCHEDULE 元数据 SeriesType 字段候选项",
        "children": [("0", "No"), ("1", "Series"), ("2", "Season Series")],
    },
    {
        "code": "Metalayout",
        "name": "Metalayout",
        "remark": "MOVIE/EPISODE 元数据 Metalayout 字段候选项，默认 0",
        "children": [("0", "Layout 0"), ("1", "Layout 1")],
    },
]


async def init_dicts(db: AsyncSession) -> None:
    """初始化数据字典（幂等，保证父子层级关系）"""
    root_map: dict[str, DictNode] = {}
    for index, item in enumerate(_PRESET_DICTS):
        existing = (
            await db.execute(select(DictNode).where(DictNode.parent_id.is_(None), DictNode.code == item["code"], DictNode.is_deleted == False))
        ).scalar_one_or_none()
        if existing is None:
            existing = DictNode(
                parent_id=None,
                code=item["code"],
                name=item["name"],
                sort_order=index,
                status=DictStatus.ACTIVE.value,
                remark=item["remark"],
                is_system=True,
            )
            db.add(existing)
            await db.flush()
        else:
            existing.name = item["name"]
            existing.sort_order = index
            existing.status = DictStatus.ACTIVE.value
            existing.remark = item["remark"]
            existing.is_system = True
        root_map[item["code"]] = existing

    for item in _PRESET_DICTS:
        parent = root_map[item["code"]]
        for child_index, (child_code, child_name) in enumerate(item["children"]):
            existing_child = (
                await db.execute(select(DictNode).where(DictNode.parent_id == parent.id, DictNode.code == child_code, DictNode.is_deleted == False))
            ).scalar_one_or_none()
            if existing_child is None:
                db.add(
                    DictNode(
                        parent_id=parent.id,
                        code=child_code,
                        name=child_name,
                        sort_order=child_index,
                        status=DictStatus.ACTIVE.value,
                        remark=None,
                        is_system=False,
                    )
                )
            else:
                existing_child.name = child_name
                existing_child.sort_order = child_index
                existing_child.status = DictStatus.ACTIVE.value
                existing_child.remark = None
    await db.commit()


# ==================== 元数据增强 - 数据源初始化 ====================

async def init_metadata_sources(db: AsyncSession) -> None:
    """初始化默认IMDb数据源（幂等，不覆盖用户已修改的值）"""
    from app.internal.cms_biz_metada.models.metadata_enhance import MetadataSource

    default_sources = [
        {
            "name": "IMDb",
            "content_type": "Movie",
            "collect_type": "网页爬取",
            "url": "https://www.imdb.com",
            "api_endpoint": None,
            "auth_type": None,
            "api_key": None,
            "rate_limit": 500,
            "status": "YES",
            "page_url_template": "https://www.imdb.com/title/{{external_id}}/",
            "render_type": "HeadlessBrowser",
            "field_extract_rules": json.dumps([
                {"key": "title", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "name"},
                {"key": "score", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "aggregateRating.ratingValue"},
                {"key": "poster_url", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "image"},
                {"key": "description", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "description"},
                {"key": "director", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "director"},
                {"key": "actors", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "actor"},
                {"key": "genre", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "genre"},
                {"key": "duration", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "duration"},
            ], ensure_ascii=False),
        },
        {
            "name": "IMDb",
            "content_type": "Series",
            "collect_type": "网页爬取",
            "url": "https://www.imdb.com",
            "api_endpoint": None,
            "auth_type": None,
            "api_key": None,
            "rate_limit": 500,
            "status": "YES",
            "page_url_template": "https://www.imdb.com/title/{{external_id}}/",
            "render_type": "HeadlessBrowser",
            "field_extract_rules": json.dumps([
                {"key": "title", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "name"},
                {"key": "score", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "aggregateRating.ratingValue"},
                {"key": "poster_url", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "image"},
                {"key": "description", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "description"},
                {"key": "director", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "director"},
                {"key": "actors", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "actor"},
                {"key": "genre", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "genre"},
            ], ensure_ascii=False),
        },
        {
            "name": "IMDb",
            "content_type": "Cast",
            "collect_type": "网页爬取",
            "url": "https://www.imdb.com",
            "api_endpoint": None,
            "auth_type": None,
            "api_key": None,
            "rate_limit": 500,
            "status": "YES",
            "page_url_template": "https://www.imdb.com/name/{{external_id}}/",
            "render_type": "HeadlessBrowser",
            "field_extract_rules": json.dumps([
                {"key": "name", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "name"},
                {"key": "photo_url", "selector": 'script[type="application/ld+json"]', "attr": "jsonld", "path": "image"},
            ], ensure_ascii=False),
        },
    ]

    for source_data in default_sources:
        existing = (
            await db.execute(
                select(MetadataSource).where(
                    MetadataSource.name == source_data["name"],
                    MetadataSource.content_type == source_data["content_type"],
                    MetadataSource.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if existing is None:
            db.add(MetadataSource(**source_data))
    await db.commit()
