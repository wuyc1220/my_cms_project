from enum import Enum
from typing import NamedTuple


class EnrichFieldType(str, Enum):
    FOREIGN_KEY = "foreign_key"
    DICT_CODE = "dict_code"
    STATUS_ENUM = "status_enum"
    DICT_CODE_ARRAY = "dict_code_array"
    FOREIGN_KEY_ARRAY = "foreign_key_array"


class FieldMapping(NamedTuple):
    field_name: str
    enrich_type: EnrichFieldType
    target_name: str
    source: str


FOREIGN_KEY_FIELDS: dict[str, list[FieldMapping]] = {
    "category": [
        FieldMapping("parent_id", EnrichFieldType.FOREIGN_KEY, "parent_name", "category"),
    ],
    "crawl_task": [
        FieldMapping("source_id", EnrichFieldType.FOREIGN_KEY, "source_name", "metadata_source"),
    ],
    "dict_node": [FieldMapping("parent_id", EnrichFieldType.FOREIGN_KEY, "parent_name", "dict_node")],
    "menu": [FieldMapping("parent_id", EnrichFieldType.FOREIGN_KEY, "parent_name", "menu")],
    "user": [
        FieldMapping("role_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "role_names", "role"),
    ],
    "provider": [
        FieldMapping("l1_assignee_id", EnrichFieldType.FOREIGN_KEY, "l1_assignee_name", "user"),
        FieldMapping("l2_assignee_id", EnrichFieldType.FOREIGN_KEY, "l2_assignee_name", "user"),
        FieldMapping("l3_assignee_id", EnrichFieldType.FOREIGN_KEY, "l3_assignee_name", "user"),
    ],
    "task": [
        FieldMapping("assignee_id", EnrichFieldType.FOREIGN_KEY, "assignee_name", "user"),
    ],
    "contract": [
        FieldMapping("provider_id", EnrichFieldType.FOREIGN_KEY, "provider_name", "provider"),
    ],
    "license": [
        FieldMapping("contract_id", EnrichFieldType.FOREIGN_KEY, "contract_name", "contract"),
    ],
    "channel_metadata": [
        FieldMapping("content_id", EnrichFieldType.FOREIGN_KEY, "content_name", "content"),
    ],
    "schedule_metadata": [
        FieldMapping("content_id", EnrichFieldType.FOREIGN_KEY, "content_name", "content"),
        FieldMapping("type_id", EnrichFieldType.FOREIGN_KEY, "type_name", "content_type"),
        FieldMapping("package_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "package_names", "package"),
        FieldMapping("tag_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "tag_names", "tag"),
    ],
    "cast_role_map": [
        FieldMapping("cast_id", EnrichFieldType.FOREIGN_KEY, "cast_name", "cast"),
    ],
    "content": [
        # 题材/标签存于中间表，名称由 API 层合并进快照（genre_names/custom_tag_names），
        # 此处不再保留已废弃的 genre_id 单值映射（genre_id 列已从主表/元数据表移除）
        FieldMapping("custom_tag_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "custom_tag_names", "custom_tag"),
        # 节目单编辑日志补频道名（channel_id 由 API 层合并自 parent_id，对齐创建日志 schedule 口径）
        FieldMapping("channel_id", EnrichFieldType.FOREIGN_KEY, "channel_name", "channel"),
    ],
    "schedule": [
        # 节目单快照补充所属频道名称（前端 schedule 标签段已有 channel_name 文案）
        FieldMapping("channel_id", EnrichFieldType.FOREIGN_KEY, "channel_name", "channel"),
    ],
    "program_metadata": [
        FieldMapping("content_id", EnrichFieldType.FOREIGN_KEY, "content_name", "content"),
        FieldMapping("type_id", EnrichFieldType.FOREIGN_KEY, "type_name", "content_type"),
        FieldMapping("tag_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "tag_names", "tag"),
    ],
    "series_metadata": [
        FieldMapping("content_id", EnrichFieldType.FOREIGN_KEY, "content_name", "content"),
        FieldMapping("type_id", EnrichFieldType.FOREIGN_KEY, "type_name", "content_type"),
        FieldMapping("tag_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "tag_names", "tag"),
    ],
    "movie": [
        FieldMapping("content_id", EnrichFieldType.FOREIGN_KEY, "content_name", "content"),
    ],
    "content_auth": [
        FieldMapping("role_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "role_names", "role"),
        FieldMapping("user_ids", EnrichFieldType.FOREIGN_KEY_ARRAY, "user_names", "user"),
    ],
}

DICT_CODE_FIELDS: dict[str, list[FieldMapping]] = {
    "category": [
        FieldMapping("platform", EnrichFieldType.DICT_CODE, "platform_name", "Platform"),
        FieldMapping("category_type", EnrichFieldType.DICT_CODE, "category_type_name", "Category_Type"),
    ],
    "provider": [
        FieldMapping("review_level", EnrichFieldType.DICT_CODE, "review_level_name", "Content_review_level"),
        FieldMapping("country", EnrichFieldType.DICT_CODE, "country_name", "Country"),
    ],
    "license": [
        FieldMapping("service_type", EnrichFieldType.DICT_CODE, "service_type_name", "ServiceType"),
        FieldMapping("regions", EnrichFieldType.DICT_CODE_ARRAY, "regions_name", "Regions"),
    ],
    "physical_channel": [
        FieldMapping("mediaservice", EnrichFieldType.DICT_CODE, "mediaservice_name", "Mediaservice"),
        FieldMapping("definition", EnrichFieldType.DICT_CODE, "definition_name", "Definition"),
        FieldMapping("videoencode", EnrichFieldType.DICT_CODE, "videoencode_name", "Videoencode"),
    ],
    "channel_metadata": [
        FieldMapping("channel_type", EnrichFieldType.DICT_CODE, "channel_type_name", "Channel_type"),
        FieldMapping("audio_type", EnrichFieldType.DICT_CODE, "audio_type_name", "AudioType"),
        FieldMapping("rating_level", EnrichFieldType.DICT_CODE, "rating_level_name", "RatingLevel"),
        FieldMapping("audio_lang", EnrichFieldType.DICT_CODE_ARRAY, "audio_lang_names", "Language"),
        FieldMapping("subtitle_lang", EnrichFieldType.DICT_CODE_ARRAY, "subtitle_lang_names", "Language"),
        FieldMapping("language", EnrichFieldType.DICT_CODE_ARRAY, "language_names", "Language"),
    ],
    "schedule_metadata": [
        FieldMapping("vod_type", EnrichFieldType.DICT_CODE_ARRAY, "vod_type_names", "VodType"),
        FieldMapping("audio_lang", EnrichFieldType.DICT_CODE_ARRAY, "audio_lang_names", "Language"),
        FieldMapping("subtitle_lang", EnrichFieldType.DICT_CODE_ARRAY, "subtitle_lang_names", "Language"),
        FieldMapping("rating_level", EnrichFieldType.DICT_CODE, "rating_level_name", "RatingLevel"),
        FieldMapping("advice", EnrichFieldType.DICT_CODE_ARRAY, "advice_names", "Advice"),
        FieldMapping("broadcast_type", EnrichFieldType.DICT_CODE, "broadcast_type_name", "BroadcastType"),
    ],
    "program_metadata": [
        FieldMapping("vod_type", EnrichFieldType.DICT_CODE_ARRAY, "vod_type_names", "VodType"),
        FieldMapping("language", EnrichFieldType.DICT_CODE, "language_name", "Language"),
        FieldMapping("audio_lang", EnrichFieldType.DICT_CODE_ARRAY, "audio_lang_names", "Language"),
        FieldMapping("subtitle_lang", EnrichFieldType.DICT_CODE_ARRAY, "subtitle_lang_names", "Language"),
        FieldMapping("rating_level", EnrichFieldType.DICT_CODE, "rating_level_name", "RatingLevel"),
        FieldMapping("advice", EnrichFieldType.DICT_CODE_ARRAY, "advice_names", "Advice"),
        FieldMapping("metalayout", EnrichFieldType.DICT_CODE, "metalayout_name", "Metalayout"),
    ],
    "series_metadata": [
        FieldMapping("vod_type", EnrichFieldType.DICT_CODE_ARRAY, "vod_type_names", "VodType"),
        FieldMapping("language", EnrichFieldType.DICT_CODE, "language_name", "Language"),
        FieldMapping("audio_lang", EnrichFieldType.DICT_CODE_ARRAY, "audio_lang_names", "Language"),
        FieldMapping("subtitle_lang", EnrichFieldType.DICT_CODE_ARRAY, "subtitle_lang_names", "Language"),
        FieldMapping("rating_level", EnrichFieldType.DICT_CODE, "rating_level_name", "RatingLevel"),
        FieldMapping("advice", EnrichFieldType.DICT_CODE_ARRAY, "advice_names", "Advice"),
        FieldMapping("metalayout", EnrichFieldType.DICT_CODE, "metalayout_name", "Metalayout"),
    ],
    "movie": [
        FieldMapping("audio_type", EnrichFieldType.DICT_CODE, "audio_type_name", "AudioType"),
        FieldMapping("screen_format", EnrichFieldType.DICT_CODE, "screen_format_name", "ScreenFormat"),
        FieldMapping("definition", EnrichFieldType.DICT_CODE, "definition_name", "Definition"),
        FieldMapping("mediaservice", EnrichFieldType.DICT_CODE, "mediaservice_name", "Mediaservice"),
    ],
}

STATUS_ENUM_FIELDS: dict[str, list[FieldMapping]] = {
    "category": [
        FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "category_status"),
    ],
    "cast": [
        FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "cast_status"),
    ],
    "metadata_source": [
        FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "metadata_source_status"),
    ],
    "crawl_task": [
        FieldMapping("crawl_status", EnrichFieldType.STATUS_ENUM, "crawl_status_label_key", "crawl_task_status"),
    ],
    "role": [FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "role_status")],
    "dict_node": [FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "dict_node_status")],
    "sensitive_word": [FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "sensitive_word_status")],
    "menu": [FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "menu_status")],
    "workflow_config": [FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "workflow_config_status")],
    "metadata_quality_check": [FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "metadata_quality_check_status")],
    "scheduled_task": [
        FieldMapping("schedule_status", EnrichFieldType.STATUS_ENUM, "schedule_status_label_key", "scheduled_task_schedule_status"),
        FieldMapping("execution_status", EnrichFieldType.STATUS_ENUM, "execution_status_label_key", "scheduled_task_execution_status"),
    ],
    "license": [
        FieldMapping("status", EnrichFieldType.STATUS_ENUM, "status_label_key", "license_status"),
    ],
}

STATUS_ENUM_VALUES: dict[str, dict[str | int, str]] = {
    "category_status": {
        0: "log.status.disabled",
        1: "log.status.enabled",
    },
    "cast_status": {
        0: "log.status.inactive",
        1: "log.status.active",
    },
    "metadata_source_status": {
        "YES": "log.status.enabled",
        "NO": "log.status.disabled",
    },
    "crawl_task_status": {
        "Created": "log.crawl_status.created",
        "InProgress": "log.crawl_status.in_progress",
        "Completed": "log.crawl_status.completed",
        "Failed": "log.crawl_status.failed",
    },
    "user_status": {"active": "log.status.active", "disabled": "log.status.disabled", "deleted": "log.status.deleted"},
    "role_status": {"active": "log.status.active", "disabled": "log.status.disabled"},
    "dict_node_status": {"active": "log.status.active", "disabled": "log.status.disabled"},
    "sensitive_word_status": {"active": "log.status.active", "disabled": "log.status.disabled"},
    "menu_status": {"active": "log.status.active", "disabled": "log.status.disabled"},
    "workflow_config_status": {"draft": "log.workflow_status.draft", "published": "log.workflow_status.published", "unpublished": "log.workflow_status.unpublished"},
    "metadata_quality_check_status": {"pending": "log.quality_check_status.pending", "running": "log.quality_check_status.running", "completed": "log.quality_check_status.completed", "failed": "log.quality_check_status.failed"},
    "scheduled_task_schedule_status": {"enabled": "log.status.enabled", "disabled": "log.status.disabled"},
    "scheduled_task_execution_status": {"idle": "log.execution_status.idle", "running": "log.execution_status.running"},
    "license_status": {"ACTIVE": "log.license_status.active", "INACTIVE": "log.license_status.inactive", "EXPIRED": "log.license_status.expired", "DELETED": "log.license_status.deleted"},
}

ENTITY_TABLE_MAP: dict[str, str] = {
    "category": "category",
    "cast": "cast",
    "metadata_source": "metadata_source",
    "crawl_task": "metadata_crawl_task",
    "genre": "genre",
    "tag": "tag",
    "custom_tag": "custom_tag",
    "content_type": "content_type",
    "poster_size": "poster_size",
    "custom_field": "custom_field",
    "user": "cms_user",
    "role": "role",
    "dict_node": "dict",
    "config": "config",
    "sensitive_word": "sensitive_word",
    "menu": "cms_menu",
    "scheduled_task": "scheduled_task",
    "metadata_quality_check": "metadata_quality_check",
    "validation_rule": "metadata_validation_rule",
    "usage_limit": "usage_limit",
    "workflow_config": "workflow_config",
    "provider": "provider",
    "contract": "contract",
    "license": "license",
    "physical_channel": "physical_channel",
    "channel_metadata": "channel_metadata",
    "schedule": "content",
    "channel": "content",
    "schedule_metadata": "schedule_metadata",
    "cast_role_map": "cast_role_map",
    "package": "package",
    "program_metadata": "program_metadata",
    "series_metadata": "series_metadata",
    "movie": "movie",
}

SENSITIVE_FIELDS: dict[str, set[str]] = {
    "user": {"password_hash"},
}
