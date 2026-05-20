"""
系统管理模块 - 数据访问层
"""
from .user_repository import (
    get_user_by_id,
    get_user_by_username,
    list_users_query,
    get_users_by_ids,
    add_user,
    delete_user_roles,
    add_user_role,
    get_roles_by_ids,
    list_roles_query,
    get_all_roles,
    get_role_by_id,
    get_role_by_code,
    add_role,
)
from .system_repository import (
    # Config
    get_config_by_id, get_config_by_key, list_configs_query, add_config, delete_config,
    # Dict
    get_dict_by_id, get_dict_by_code, get_root_dicts, get_dict_children, add_dict, delete_dict,
    # OperationLog
    list_operation_logs_query, add_operation_log,
    # SensitiveWord
    get_sensitive_word_by_id, list_sensitive_words_query, get_all_sensitive_words,
    get_sensitive_words_by_ids, add_sensitive_word, delete_sensitive_word,
    # UsageLimit
    get_usage_limit_by_id, get_usage_limit_by_key, list_usage_limits_query,
    get_all_usage_limits, add_usage_limit, delete_usage_limit,
)

from .menu_repository import (
    get_menu_by_id, get_all_menus, get_active_menus, get_menus_by_ids,
    add_menu, update_menu, delete_menu, get_menu_children,
    get_menu_ids_by_role_id, get_menu_ids_by_role_ids, get_menu_ids_by_user_roles,
    replace_role_menus, has_role_menu_ref,
)

__all__ = [
    'get_user_by_id', 'get_user_by_username', 'list_users_query',
    'get_users_by_ids', 'add_user', 'delete_user_roles', 'add_user_role',
    'get_roles_by_ids', 'list_roles_query', 'get_all_roles',
    'get_role_by_id', 'get_role_by_code', 'add_role',
    'get_config_by_id', 'get_config_by_key', 'list_configs_query', 'add_config', 'delete_config',
    'get_dict_by_id', 'get_dict_by_code', 'get_root_dicts', 'get_dict_children', 'add_dict', 'delete_dict',
    'list_operation_logs_query', 'add_operation_log',
    'get_sensitive_word_by_id', 'list_sensitive_words_query', 'get_all_sensitive_words',
    'get_sensitive_words_by_ids', 'add_sensitive_word', 'delete_sensitive_word',
    'get_usage_limit_by_id', 'get_usage_limit_by_key', 'list_usage_limits_query',
    'get_all_usage_limits', 'add_usage_limit', 'delete_usage_limit',
    'get_menu_by_id', 'get_all_menus', 'get_active_menus', 'get_menus_by_ids',
    'add_menu', 'update_menu', 'delete_menu', 'get_menu_children',
    'get_menu_ids_by_role_id', 'get_menu_ids_by_role_ids', 'get_menu_ids_by_user_roles',
    'replace_role_menus', 'has_role_menu_ref',
]